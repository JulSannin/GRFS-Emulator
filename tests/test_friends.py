"""The friends screen, driven as two games would drive it.

This is the path to co-op: Future Soldier finds a friend's game by asking which sessions its
contacts are in, so with an empty contact list a hosted game cannot be found at all.
"""

import os
import unittest

from grfs import friends as model
from grfs.backend import Backend, PRESENCE_WINDOW
from grfs.config import Config
from grfs.net import PrudpServer
from grfs.protocols import (FRIENDS, FRIEND_MESSAGES_EXT, SEARCH_FRIENDS, TICKET_GRANTING,
                            UBI_ACCOUNT_MANAGEMENT, UBI_FRIENDS)
from grfs.rmc.stream import Reader

from tests.test_login import FakeTransport, GameSide, read_login_reply


class FriendsScreen(unittest.TestCase):
    def setUp(self, auto=False):
        config = Config(path=os.devnull, overrides={"auto_friends": "yes" if auto else "no"})
        self.backend = Backend(config=config, data_dir="")
        self.server = PrudpServer(self.backend, "secure", self.backend.secure_port)
        self.server.transport = FakeTransport()
        self.schema = self.backend.schema
        self.a = self.sign_in("Nay1012", 2347)
        self.b = self.sign_in("NayNik", 2348)

    def sign_in(self, name, port):
        game = GameSide(self.server, ("127.0.0.1", port))
        game.handshake()
        method = self.schema.method("TicketGrantingProtocol", 2)
        _, reply, _ = game.call(TICKET_GRANTING, 2, self.schema.encode_request(method, {
            "strUserName": name,
            "oExtraData": {"__class__": "UbiAuthenticationLoginCustomData",
                           "m_userName": name, "m_onlineKey": "", "m_password": ""}}))
        game.name = name
        game.pid = read_login_reply(reply.body)["pid"]
        return game

    def call(self, game, protocol, protocol_name, method_id, args=None):
        method = self.schema.method(protocol_name, method_id)
        params = self.schema.encode_request(method, args or {})
        _, reply, _ = game.call(protocol, method_id, params)
        self.assertTrue(reply.success, "%s.%s refused with 0x%08X"
                        % (protocol_name, method.name, reply.error))
        return method, reply.body

    def read(self, method, body, name):
        """One output of a reply, by its name in the schema."""
        r = Reader(body)
        if method.ret is not None:
            self.schema.read(r, method.ret)
        for param in method.outputs:
            value = self.schema.read(r, param.type)
            if param.name == name:
                return value
        raise KeyError(name)

    # ----- the flow --------------------------------------------------------------------------
    def test_search_finds_the_other_player_and_not_yourself(self):
        method, body = self.call(self.a, SEARCH_FRIENDS, "SearchFriendsProtocol", 1,
                                 {"partOfName": "nay"})
        found = self.read(method, body, "foundList")
        self.assertEqual([(f["m_strName"], f["m_pid"]) for f in found],
                         [("NayNik", self.b.pid)])

    def test_a_request_has_two_sides(self):
        self.call(self.a, FRIENDS, "FriendsProtocol", 2,
                  {"strPlayerName": "NayNik", "uiDetails": 1, "strMessage": "go co-op?"})

        # The one who asked sees a request they sent; the other an invitation to answer.
        method, body = self.call(self.a, FRIENDS, "FriendsProtocol", 13, {"resultRange":
                                 {"m_uiOffset": 0, "m_uiSize": 200}})
        mine = self.read(method, body, "lstRelationshipsList")
        self.assertEqual([(e["m_strName"], e["m_byRelationship"]) for e in mine],
                         [("NayNik", model.REQUEST_SENT)])

        method, body = self.call(self.b, FRIENDS, "FriendsProtocol", 13, {"resultRange":
                                 {"m_uiOffset": 0, "m_uiSize": 200}})
        waiting = self.read(method, body, "lstRelationshipsList")
        self.assertEqual([(e["m_strName"], e["m_byRelationship"], e["m_byStatus"])
                          for e in waiting],
                         [("Nay1012", model.REQUEST_RECEIVED, 1)])

        # GetDetailedList asked about 3 is the BLOCKLIST, so the request is not in it: that is
        # where every request used to show a second time.
        method, body = self.call(self.b, FRIENDS, "FriendsProtocol", 12,
                                 {"byRelationship": 3, "bReversed": False})
        self.assertEqual(self.read(method, body, "lstFriendsList"), [])

        # The message travels with it.
        method, body = self.call(self.b, FRIEND_MESSAGES_EXT,
                                 "FriendMessagesExtensionServiceProtocol", 2)
        messages = self.read(method, body, "messages")
        self.assertEqual([(m["m_senderPid"], m["m_message"]) for m in messages],
                         [(self.a.pid, "go co-op?")])

        # Asking about friends while only a request is outstanding must answer "none" - this is
        # a filter, not a label to echo back.
        method, body = self.call(self.b, FRIENDS, "FriendsProtocol", 12,
                                 {"byRelationship": model.FRIEND, "bReversed": False})
        self.assertEqual(self.read(method, body, "lstFriendsList"), [])

    def test_accepting_makes_both_friends(self):
        self.call(self.a, FRIENDS, "FriendsProtocol", 2,
                  {"strPlayerName": "NayNik", "uiDetails": 1, "strMessage": ""})
        self.call(self.b, FRIENDS, "FriendsProtocol", 5, {"uiPlayer": self.a.pid})
        for game, other, pid in ((self.a, "NayNik", self.b.pid), (self.b, "Nay1012", self.a.pid)):
            method, body = self.call(game, FRIENDS, "FriendsProtocol", 12,
                                     {"byRelationship": model.FRIEND, "bReversed": False})
            friends = self.read(method, body, "lstFriendsList")
            self.assertEqual([(f["m_strName"], f["m_pid"], f["m_strStatus"]) for f in friends],
                             [(other, pid, "Online")])
            # GetList answers the same question with pids only.
            method, body = self.call(game, FRIENDS, "FriendsProtocol", 11,
                                     {"byRelationship": model.FRIEND, "bReversed": False})
            self.assertEqual(self.read(method, body, "lstFriendsList"), [pid])

    def test_the_total_count_comes_before_the_list(self):
        """The bug that kept the screen empty: without the count the client read the list's
        length out of the first entry's pid."""
        self.befriend()
        method, body = self.call(self.a, FRIENDS, "FriendsProtocol", 13,
                                 {"resultRange": {"m_uiOffset": 0, "m_uiSize": 200}})
        r = Reader(body)
        self.assertEqual(r.u32(), 1, "uiTotalCount")
        self.assertEqual(r.u32(), 1, "the list's own count")
        self.assertEqual(r.u32(), self.b.pid)

    def test_the_uplay_tab_resolves_its_names(self):
        self.befriend()
        method, body = self.call(self.a, UBI_FRIENDS, "UbiFriendsProtocol", 7)
        rows = self.read(method, body, "relationshipsList")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["m_from"]["m_principalId"], self.a.pid)
        self.assertEqual(rows[0]["m_to"]["m_principalId"], self.b.pid)
        self.assertEqual(rows[0]["m_relationship"], model.FRIEND)
        self.assertFalse(rows[0]["m_onBlacklist"])

        # An entry carries no name, so the client asks for them with the ids it just got back.
        ids = [rows[0]["m_from"]["m_ubiAccountId"], rows[0]["m_to"]["m_ubiAccountId"]]
        method, body = self.call(self.a, UBI_ACCOUNT_MANAGEMENT,
                                 "UbiAccountManagementProtocol", 14, {"UbiAccountIds": ids})
        names = dict(self.read(method, body, "Usernames"))
        self.assertEqual(names, {"Nay1012": "Nay1012", "NayNik": "NayNik"})

    def test_a_quiet_player_is_offline_but_stays_in_the_list(self):
        self.befriend()
        other = self.backend.clients.by_pid(self.b.pid)
        other.last_seen -= PRESENCE_WINDOW + 1
        method, body = self.call(self.a, FRIENDS, "FriendsProtocol", 13,
                                 {"resultRange": {"m_uiOffset": 0, "m_uiSize": 200}})
        entries = self.read(method, body, "lstRelationshipsList")
        self.assertEqual([(e["m_strName"], e["m_byStatus"]) for e in entries],
                         [("NayNik", 0)])

    def test_removing_a_friend_keeps_the_account(self):
        self.befriend()
        self.call(self.a, FRIENDS, "FriendsProtocol", 9, {"uiPlayer": self.b.pid})
        self.assertEqual(self.backend.friends.contacts("Nay1012"), [])
        self.assertEqual(self.backend.friends.contacts("NayNik"), [])
        # Still registered, still findable, so it can be undone.
        method, body = self.call(self.a, SEARCH_FRIENDS, "SearchFriendsProtocol", 1,
                                 {"partOfName": "NayNik"})
        self.assertEqual(len(self.read(method, body, "foundList")), 1)
        self.assertEqual(self.backend.accounts.get("NayNik").pid, self.b.pid)

    def test_blocking_ends_the_friendship_and_moves_them_to_the_blocklist(self):
        self.befriend()
        self.call(self.a, FRIENDS, "FriendsProtocol", 7, {"uiPlayer": self.b.pid,
                                                          "uiDetails": 0})
        method, body = self.call(self.a, FRIENDS, "FriendsProtocol", 12,
                                 {"byRelationship": 3, "bReversed": False})
        self.assertEqual([(e["m_strName"], e["m_pid"]) for e in
                          self.read(method, body, "lstFriendsList")], [("NayNik", self.b.pid)])
        # Not on the Uplay side, where it would become an "offline" friend as well, and not in
        # the contact list with rel 0, where it would be drawn as an ordinary friend.
        method, body = self.call(self.a, UBI_FRIENDS, "UbiFriendsProtocol", 7)
        self.assertEqual(self.read(method, body, "relationshipsList"), [])
        method, body = self.call(self.a, FRIENDS, "FriendsProtocol", 13,
                                 {"resultRange": {"m_uiOffset": 0, "m_uiSize": 200}})
        self.assertEqual(self.read(method, body, "lstRelationshipsList"), [])
        # And the one who was blocked no longer has them as a friend either.
        self.assertEqual(self.backend.friends.contacts("NayNik"), [])
        self.assertEqual(self.backend.friends.blacklist("NayNik"), [])

    def test_asking_somebody_who_already_asked_you_means_yes(self):
        self.call(self.a, FRIENDS, "FriendsProtocol", 2,
                  {"strPlayerName": "NayNik", "uiDetails": 1, "strMessage": ""})
        self.call(self.b, FRIENDS, "FriendsProtocol", 2,
                  {"strPlayerName": "Nay1012", "uiDetails": 1, "strMessage": ""})
        self.assertEqual(self.backend.friends.relationship("Nay1012", "NayNik"), model.FRIEND)
        self.assertEqual(self.backend.friends.relationship("NayNik", "Nay1012"), model.FRIEND)

    def test_adding_somebody_who_is_not_here_answers_no(self):
        """The call declares a bool, so "no such player" is false - not a failed call."""
        method, body = self.call(self.a, FRIENDS, "FriendsProtocol", 2,
                                 {"strPlayerName": "nobody", "uiDetails": 1, "strMessage": ""})
        self.assertFalse(Reader(body).bool())
        # And nothing is left behind pointing at a player who does not exist.
        self.assertIsNone(self.backend.friends.edge("Nay1012", "nobody"))

    def befriend(self):
        self.backend.friends.request("Nay1012", "NayNik")
        self.backend.friends.accept("NayNik", "Nay1012")


class EveryoneIsAFriend(FriendsScreen):
    """auto_friends: a private server for two people can skip the add-and-accept dance."""

    def setUp(self):
        super().setUp(auto=True)

    def test_they_see_each_other_without_asking(self):
        for game, other in ((self.a, "NayNik"), (self.b, "Nay1012")):
            method, body = self.call(game, FRIENDS, "FriendsProtocol", 12,
                                     {"byRelationship": model.FRIEND, "bReversed": False})
            friends = self.read(method, body, "lstFriendsList")
            self.assertEqual([f["m_strName"] for f in friends], [other])

    # The rest of the flow is about asking and answering, which this mode skips.
    test_a_request_has_two_sides = None
    test_accepting_makes_both_friends = None
    test_removing_a_friend_keeps_the_account = None
    test_blocking_ends_the_friendship_and_moves_them_to_the_blocklist = None
    test_asking_somebody_who_already_asked_you_means_yes = None
    test_a_quiet_player_is_offline_but_stays_in_the_list = None


if __name__ == "__main__":
    unittest.main()


class StaleRecords(unittest.TestCase):
    """A game that restarts comes back from a different port, and the old record is dead."""

    def setUp(self):
        self.backend = Backend(config=Config(path=os.devnull), data_dir="")
        self.server = PrudpServer(self.backend, "secure", self.backend.secure_port)
        self.server.transport = FakeTransport()
        self.schema = self.backend.schema

    def sign_in(self, name, port):
        game = GameSide(self.server, ("127.0.0.1", port))
        game.handshake()
        method = self.schema.method("TicketGrantingProtocol", 2)
        game.call(TICKET_GRANTING, 2, self.schema.encode_request(method, {
            "strUserName": name,
            "oExtraData": {"__class__": "UbiAuthenticationLoginCustomData",
                           "m_userName": name, "m_onlineKey": "", "m_password": ""}}))
        return game

    def test_logging_in_again_replaces_the_earlier_record(self):
        self.sign_in("Nay1012", 2347)
        self.assertEqual(len(self.backend.clients), 1)
        self.sign_in("Nay1012", 5000)
        self.assertEqual(len(self.backend.clients), 1)
        self.assertEqual(self.backend.clients.by_pid(0x1234).endpoint, ("127.0.0.1", 5000))

    def test_presence_follows_the_record_that_is_alive(self):
        first = self.sign_in("Nay1012", 2347)
        client = self.backend.clients.by_signature(first.server_signature)
        client.last_seen -= PRESENCE_WINDOW + 1
        self.assertFalse(self.backend.is_online(0x1234))
        self.sign_in("Nay1012", 5000)
        self.assertTrue(self.backend.is_online(0x1234))

    def test_quiet_records_are_forgotten(self):
        self.sign_in("Nay1012", 2347)
        for client in self.backend.clients:
            client.last_seen -= 10_000
        self.assertEqual(len(self.backend.forget_quiet_clients()), 1)
        self.assertEqual(len(self.backend.clients), 0)
