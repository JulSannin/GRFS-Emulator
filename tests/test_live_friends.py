"""The friends screen against a running server, two or three games at once."""

import queue
import shutil
import struct
import tempfile
import time
import unittest

from grfs import friends as model
from grfs.backend import PRESENCE_WINDOW
from grfs.fakegame import RmcFailure
from grfs.harness import Harness
from grfs.rmc.router import Errors
from grfs.services.rich_presence import readable

from tests.live import LiveTest

F = "FriendsProtocol"
U = "UbiFriendsProtocol"
A = "UbiAccountManagementProtocol"
M = "FriendMessagesExtensionServiceProtocol"
R = "RichPresenceProtocol"

BLACKLIST = 3          # what byRelationship 3 means to GetDetailedList: the BLOCKLIST tab


def silence(test, player):
    """Make a player go quiet, as a game that was closed does: nothing more leaves its socket, and
    its last packet is longer ago than the presence window. Returns a function that undoes the
    first half."""
    mute = lambda pkt: True                                       # noqa: E731
    player.wire.drop_out.append(mute)
    time.sleep(0.1)                     # let whatever was already on its way arrive

    def backdate():
        client = test.server.backend.clients.by_pid(player.pid)
        client.last_seen -= PRESENCE_WINDOW + 1
    test.on_server(backdate)
    return lambda: player.wire.drop_out.remove(mute)


def change(by, kind=model.SOMETHING_CHANGED):
    """The notification a player gets when `by` changed something on their lists."""
    return {"m_pidSource": by.pid, "m_uiType": 1000, "m_uiParam1": by.pid, "m_uiParam2": kind,
            "m_strParam": "", "m_uiParam3": 0}


class Social(LiveTest):
    """Shortcuts shared by the classes below."""

    def ask(self, who, whom, text="co-op?", details=1):
        return who.call(F, "AddFriendByName", {"strPlayerName": whom.name, "uiDetails": details,
                                               "strMessage": text})["retval"]

    def befriend(self, one, two):
        self.assertTrue(self.ask(one, two))
        self.assertTrue(two.call(F, "AcceptFriendship", {"uiPlayer": one.pid})["retval"])
        self.drain(one, two)

    def detailed(self, who, relationship):
        return {r["m_strName"]: r for r in who.call(F, "GetDetailedList", {
            "byRelationship": relationship, "bReversed": False})["lstFriendsList"]}

    def blocklist(self, who):
        return sorted(self.detailed(who, BLACKLIST))

    def uplay(self, who):
        return [(r["m_to"]["m_ubiAccountId"], r["m_relationship"], r["m_onBlacklist"])
                for r in who.call(U, "GetRelationshipsList")["relationshipsList"]]

    def told(self, player, timeout=3.0) -> dict:
        protocol, method, args = player.next_server_call(timeout)
        self.assertEqual((protocol, method), ("NotificationProtocol", "ProcessNotificationEvent"))
        return args["oEvent"]

    def not_told(self, *players, wait=0.3):
        """Nothing arrived for these players - given the time a notification takes to arrive."""
        time.sleep(wait)
        for player in players:
            with self.assertRaises(queue.Empty, msg="%s was told something" % player.name):
                player.next_server_call(timeout=0)

    def drain(self, *players):
        time.sleep(0.2)
        for player in players:
            while True:
                try:
                    player.next_server_call(timeout=0)
                except queue.Empty:
                    break


class Friendship(Social):
    def setUp(self):
        super().setUp()
        self.alice = self.player("alice")
        self.bob = self.player("bob")

    def test_asking_and_accepting(self):
        found = self.alice.call("SearchFriendsProtocol", "SearchFriends", {"partOfName": "B"})
        self.assertEqual(found["foundList"], [{"m_pid": self.bob.pid, "m_strName": "bob"}])

        self.assertTrue(self.ask(self.alice, self.bob))
        self.assertEqual(self.alice.relationships(), {"bob": (model.REQUEST_SENT, True)})
        # INVITATIONS is GetRelationships' rel 3, and only that: GetDetailedList asked about 3
        # is the BLOCKLIST, where a request used to show a second time.
        self.assertEqual(self.bob.relationships(), {"alice": (model.REQUEST_RECEIVED, True)})
        self.assertEqual(self.blocklist(self.bob), [])
        self.assertEqual(self.blocklist(self.alice), [])
        self.assertEqual(self.detailed(self.bob, 1), {})
        self.assertEqual(list(self.detailed(self.alice, 2)), ["bob"])
        self.assertEqual(self.bob.call(M, "ReceiveFriendInvitationMessages"),
                         {"messages": [{"m_senderPid": self.alice.pid, "m_message": "co-op?"}]})

        self.assertTrue(self.bob.call(F, "AcceptFriendship", {"uiPlayer": self.alice.pid})["retval"])
        self.assertEqual(self.alice.relationships(), {"bob": (model.FRIEND, True)})
        self.assertEqual(self.bob.relationships(), {"alice": (model.FRIEND, True)})
        self.assertEqual(self.detailed(self.bob, 1)["alice"]["m_strStatus"], "Online")
        self.assertEqual(self.alice.call(F, "GetList", {"byRelationship": 1})["lstFriendsList"],
                         [self.bob.pid])
        self.assertEqual(self.alice.call(F, "GetList", {"byRelationship": 0})["lstFriendsList"],
                         [self.bob.pid])
        self.assertEqual(self.alice.call(F, "GetList", {"byRelationship": 3})["lstFriendsList"],
                         [])
        self.assertEqual(self.bob.call(M, "ReceiveFriendInvitationMessages"), {"messages": []})

    def test_each_side_is_told_what_the_other_did(self):
        self.ask(self.alice, self.bob)
        self.assertEqual(self.told(self.bob), change(self.alice, model.NEW_REQUEST))
        self.not_told(self.alice)

        self.bob.call(F, "AcceptFriendship", {"uiPlayer": self.alice.pid})
        self.assertEqual(self.told(self.alice), change(self.bob))
        self.not_told(self.bob)

        self.alice.call(F, "ClearRelationship", {"uiPlayer": self.bob.pid})
        self.assertEqual(self.told(self.bob), change(self.alice))
        self.not_told(self.alice)

    def test_declining(self):
        self.ask(self.alice, self.bob)
        self.drain(self.bob)
        self.assertTrue(self.bob.call(F, "DeclineFriendship", {"uiPlayer": self.alice.pid})["retval"])
        self.assertEqual(self.alice.relationships(), {})
        self.assertEqual(self.bob.relationships(), {})
        self.assertEqual(self.told(self.alice), change(self.bob))
        self.not_told(self.bob)

    def test_declining_is_for_requests_only(self):
        self.befriend(self.alice, self.bob)
        self.assertFalse(self.bob.call(F, "DeclineFriendship", {"uiPlayer": self.alice.pid})["retval"])
        self.assertEqual(self.bob.relationships(), {"alice": (model.FRIEND, True)})
        self.not_told(self.alice, self.bob)

    def test_cancelling_a_request(self):
        self.ask(self.alice, self.bob)
        self.drain(self.bob)
        self.assertTrue(self.alice.call(F, "ClearRelationship", {"uiPlayer": self.bob.pid})["retval"])
        self.assertEqual(self.bob.relationships(), {})
        self.assertEqual(self.bob.call(M, "ReceiveFriendInvitationMessages"), {"messages": []})
        self.assertEqual(self.told(self.bob), change(self.alice))

    def test_accepting_what_nobody_asked_is_refused(self):
        self.assertFalse(self.bob.call(F, "AcceptFriendship", {"uiPlayer": self.alice.pid})["retval"])
        self.not_told(self.alice)

    def test_removing_and_adding_back(self):
        self.befriend(self.alice, self.bob)
        self.assertTrue(self.alice.call(F, "ClearRelationship", {"uiPlayer": self.bob.pid})["retval"])
        self.assertEqual(self.bob.relationships(), {})
        self.assertEqual(self.told(self.bob), change(self.alice))
        self.assertFalse(self.alice.call(F, "ClearRelationship", {"uiPlayer": self.bob.pid})["retval"])
        self.not_told(self.bob)
        # Still findable, so it can be undone.
        found = self.alice.call("SearchFriendsProtocol", "SearchFriends", {"partOfName": "bob"})
        self.assertEqual(len(found["foundList"]), 1)
        self.assertTrue(self.ask(self.alice, self.bob))

    def test_asking_back_means_yes(self):
        self.ask(self.alice, self.bob)
        self.drain(self.bob)
        self.assertTrue(self.ask(self.bob, self.alice))
        self.assertEqual(self.alice.relationships(), {"bob": (model.FRIEND, True)})
        # Told once, not once for the request and once for the answer.
        self.assertEqual(self.told(self.alice), change(self.bob))
        self.not_told(self.alice, self.bob)

    def test_asking_twice_keeps_the_text(self):
        self.ask(self.alice, self.bob, "co-op?")
        self.ask(self.alice, self.bob, "")
        self.assertEqual(self.bob.call(M, "ReceiveFriendInvitationMessages")["messages"],
                         [{"m_senderPid": self.alice.pid, "m_message": "co-op?"}])

    def test_asking_somebody_who_was_never_here(self):
        self.assertFalse(self.alice.call(F, "AddFriendByName", {"strPlayerName": "ghost"})["retval"])
        self.assertFalse(self.alice.call(F, "AddFriend", {"uiPlayer": 0x7777})["retval"])
        self.assertFalse(self.ask(self.alice, self.alice))
        self.assertEqual(self.alice.relationships(), {})
        self.not_told(self.alice)

    def test_the_other_ways_of_asking(self):
        self.assertTrue(self.alice.call(F, "AddFriend", {"uiPlayer": self.bob.pid,
                                                         "uiDetails": 4})["retval"])
        self.assertEqual(self.bob.relationships(), {"alice": (model.REQUEST_RECEIVED, True)})
        self.alice.call(F, "ClearRelationship", {"uiPlayer": self.bob.pid})

        entry = self.alice.call(F, "AddFriendWithDetails", {"uiPlayer": self.bob.pid,
                                                            "uiDetails": 1})["relationshipData"]
        self.assertEqual((entry["m_pid"], entry["m_strName"], entry["m_byRelationship"],
                          entry["m_byStatus"]), (self.bob.pid, "bob", model.REQUEST_SENT, 1))
        self.alice.call(F, "ClearRelationship", {"uiPlayer": self.bob.pid})

        entry = self.alice.call(F, "AddFriendByNameWithDetails",
                                {"strPlayerName": "bob", "uiDetails": 1})["relationshipData"]
        self.assertEqual(entry["m_byRelationship"], model.REQUEST_SENT)
        entry = self.alice.call(F, "AddFriendByNameWithDetails",
                                {"strPlayerName": "ghost"})["relationshipData"]
        self.assertEqual((entry["m_pid"], entry["m_byRelationship"]), (0, model.NOT_A_FRIEND))

    def test_details_are_remembered(self):
        self.ask(self.alice, self.bob)
        self.assertTrue(self.alice.call(F, "UpdateDetails", {"uiPlayer": self.bob.pid,
                                                             "uiDetails": 42})["retval"])
        rows = self.alice.call(F, "GetRelationships", {"resultRange": {"m_uiSize": 10}})
        self.assertEqual(rows["lstRelationshipsList"][0]["m_uiDetails"], 42)

    def test_a_page_of_the_list_carries_the_total(self):
        carol = self.player("carol")
        for other in (self.bob, carol):
            self.ask(self.alice, other)
        page = self.alice.call(F, "GetRelationships",
                               {"resultRange": {"m_uiOffset": 1, "m_uiSize": 1}})
        self.assertEqual(page["uiTotalCount"], 2)
        self.assertEqual([r["m_strName"] for r in page["lstRelationshipsList"]], ["carol"])

    def test_presence_follows_the_last_packet(self):
        self.befriend(self.alice, self.bob)
        unmute = silence(self, self.bob)
        self.assertEqual(self.alice.relationships(), {"bob": (model.FRIEND, False)})
        self.assertEqual(self.detailed(self.alice, 1)["bob"]["m_strStatus"], "Offline")
        # A single ping brings them back.
        unmute()
        self.bob.secure.ping()
        self.assertEqual(self.alice.relationships(), {"bob": (model.FRIEND, True)})

    def test_somebody_who_never_came_back_stays_in_the_list(self):
        self.befriend(self.alice, self.bob)
        self.on_server(lambda: self.server.backend.clients.drop(
            self.server.backend.clients.by_pid(self.bob.pid)))
        self.assertEqual(self.alice.relationships(), {"bob": (model.FRIEND, False)})
        # And telling them anything is skipped, not an error.
        self.alice.call(F, "ClearRelationship", {"uiPlayer": self.bob.pid})
        self.assertTrue(self.wait_logged("could not tell bob about alice"))


class Blocking(Social):
    def setUp(self):
        super().setUp()
        self.alice = self.player("alice")
        self.bob = self.player("bob")

    def block(self, who, whom):
        return who.call(F, "BlackList", {"uiPlayer": whom.pid, "uiDetails": 0})["retval"]

    def test_a_blocked_friend_moves_to_the_blocklist_and_nowhere_else(self):
        self.befriend(self.alice, self.bob)
        self.assertTrue(self.block(self.alice, self.bob))
        self.assertEqual(self.blocklist(self.alice), ["bob"])
        row = self.detailed(self.alice, BLACKLIST)["bob"]
        self.assertEqual((row["m_pid"], row["m_byRelationship"]), (self.bob.pid, BLACKLIST))
        # Not a friend with rel 0 in the contact list, and not an offline friend on the Uplay
        # side either - either one draws them twice.
        self.assertEqual(self.alice.relationships(), {})
        self.assertEqual(self.uplay(self.alice), [])
        self.assertEqual(self.detailed(self.alice, 1), {})
        self.assertEqual(self.alice.call(F, "GetList", {"byRelationship": 3})["lstFriendsList"],
                         [self.bob.pid])

    def test_who_was_blocked_loses_the_blocker_everywhere_and_is_told(self):
        self.befriend(self.alice, self.bob)
        self.block(self.alice, self.bob)
        self.assertEqual(self.told(self.bob), change(self.alice))
        self.not_told(self.alice)
        self.assertEqual(self.bob.relationships(), {})
        self.assertEqual(self.blocklist(self.bob), [])
        self.assertEqual(self.uplay(self.bob), [])
        self.assertEqual(self.detailed(self.bob, 0), {})
        # And cannot ask again.
        self.assertFalse(self.ask(self.bob, self.alice))
        self.assertEqual(self.alice.relationships(), {})
        self.not_told(self.alice)

    def test_a_request_from_somebody_blocked_goes_too(self):
        self.ask(self.bob, self.alice)
        self.drain(self.alice)
        self.block(self.alice, self.bob)
        self.assertEqual(self.alice.call(M, "ReceiveFriendInvitationMessages"), {"messages": []})
        self.assertEqual(self.bob.relationships(), {})
        self.assertEqual(self.told(self.bob), change(self.alice))

    def test_blocking_a_stranger_tells_them_nothing(self):
        """Nothing on their lists changed - the blocker was never on them."""
        self.block(self.alice, self.bob)
        self.assertEqual(self.blocklist(self.alice), ["bob"])
        self.not_told(self.bob, self.alice)

    def test_unblock_is_a_friend_request_by_name(self):
        """What the BLOCKLIST tab's Unblock sends: AddFriendByName(name, 0, "") and nothing
        else (DLL 1041D3A4 -> A49770 -> TaskRDV_AddFriend)."""
        self.befriend(self.alice, self.bob)
        self.block(self.alice, self.bob)
        self.drain(self.bob)
        self.assertTrue(self.alice.call(F, "AddFriendByName", {
            "strPlayerName": "bob", "uiDetails": 0, "strMessage": ""})["retval"])
        self.assertEqual(self.blocklist(self.alice), [])
        self.assertEqual(self.alice.relationships(), {"bob": (model.REQUEST_SENT, True)})
        self.assertEqual(self.bob.relationships(), {"alice": (model.REQUEST_RECEIVED, True)})
        self.assertEqual(self.told(self.bob), change(self.alice, model.NEW_REQUEST))
        self.not_told(self.alice)

    def test_remove_on_the_blocklist_lifts_the_block(self):
        self.block(self.alice, self.bob)
        self.assertTrue(self.alice.call(F, "ClearRelationship", {"uiPlayer": self.bob.pid})["retval"])
        self.assertEqual(self.blocklist(self.alice), [])
        self.assertEqual(self.alice.relationships(), {})
        self.not_told(self.bob)
        # Now they may ask.
        self.assertTrue(self.ask(self.bob, self.alice))
        self.assertEqual(self.told(self.alice), change(self.bob, model.NEW_REQUEST))
        self.assertFalse(self.alice.call(F, "ClearRelationship", {"uiPlayer": 0x7777})["retval"])

    def test_both_blocking_each_other(self):
        self.block(self.alice, self.bob)
        self.block(self.bob, self.alice)
        self.assertEqual(self.blocklist(self.alice), ["bob"])
        self.assertEqual(self.blocklist(self.bob), ["alice"])
        # alice's Unblock lifts her own block; bob's still stands, so no request goes out.
        self.assertFalse(self.ask(self.alice, self.bob, ""))
        self.assertEqual(self.blocklist(self.alice), [])
        self.assertEqual(self.blocklist(self.bob), ["alice"])
        self.assertEqual(self.bob.relationships(), {})
        self.not_told(self.alice, self.bob)

    def test_blocking_by_name(self):
        self.assertTrue(self.alice.call(F, "BlackListByName", {"strPlayerName": "bob"})["retval"])
        self.assertFalse(self.alice.call(F, "BlackListByName", {"strPlayerName": "ghost"})["retval"])
        self.assertFalse(self.block(self.alice, self.alice))
        self.assertFalse(self.ask(self.bob, self.alice))
        self.assertEqual(self.blocklist(self.alice), ["bob"])

    def test_the_uplay_blacklist_calls_do_the_same(self):
        info = {"to": {"m_principalId": self.bob.pid, "m_ubiAccountId": "bob"}}
        self.befriend(self.alice, self.bob)
        self.alice.call(U, "AddToBlacklist", info)
        self.assertEqual(self.blocklist(self.alice), ["bob"])
        self.assertEqual(self.uplay(self.alice), [])
        self.assertEqual(self.told(self.bob), change(self.alice))
        self.alice.call(U, "RemoveFromBlacklist", info)
        self.assertEqual(self.blocklist(self.alice), [])
        # Unblocked, not befriended again: the friendship ended with the block.
        self.assertEqual(self.alice.relationships(), {})
        self.not_told(self.bob)
        self.assertTrue(self.ask(self.bob, self.alice))

    def test_the_blocklist_survives_a_restart(self):
        data = tempfile.mkdtemp(prefix="grfs-test-")
        self.addCleanup(shutil.rmtree, data, True)
        with Harness(data_dir=data) as first:
            alice, bob, carol = (first.player(n) for n in ("alice", "bob", "carol"))
            alice.call(F, "BlackList", {"uiPlayer": bob.pid})
            carol.call(F, "AddFriendByName", {"strPlayerName": "alice", "strMessage": "hi"})
            first.assert_clean()
        with Harness(data_dir=data) as second:
            alice = second.player("alice")
            self.assertEqual(self.blocklist(alice), ["bob"])
            self.assertEqual(alice.relationships(), {"carol": (model.REQUEST_RECEIVED, False)})
            self.assertEqual(alice.call(M, "ReceiveFriendInvitationMessages")["messages"][0]
                             ["m_message"], "hi")
            bob = second.player("bob")
            self.assertFalse(self.ask(bob, alice))
            second.assert_clean()


class SigningIn(Social):
    def test_friends_are_told_when_a_player_signs_in(self):
        alice = self.player("alice")
        bob = self.player("bob")
        carol = self.player("carol")
        self.befriend(alice, bob)
        self.ask(carol, bob)                  # a request is not a friend: carol is not told
        self.drain(alice, bob, carol)

        again = self.player("bob")            # the game restarted
        self.assertEqual(self.told(alice), change(again))
        self.not_told(carol, again)
        self.assertTrue(self.logged("bob is here: told alice"))

    def test_nobody_is_told_about_somebody_without_friends(self):
        alice = self.player("alice")
        self.player("bob")
        self.not_told(alice)

    def test_friends_are_told_when_a_player_goes_quiet(self):
        """Silence past the presence window is the sign-out; the housekeeping that notices it
        tells the friends once, so their screens refetch and show the player offline."""
        alice = self.player("alice")
        bob = self.player("bob")
        carol = self.player("carol")
        self.befriend(alice, bob)
        self.drain(alice, bob, carol)
        unmute = silence(self, bob)
        went = self.on_server(self.server.backend.notice_departures)
        self.assertEqual([c.name for c in went], ["bob"])
        self.assertEqual(self.told(alice), change(bob))
        self.not_told(carol)
        self.assertTrue(self.logged("bob is gone: told alice"))
        self.assertEqual(alice.relationships(), {"bob": (model.FRIEND, False)})
        # Once is enough: the next look finds nothing new.
        self.assertEqual(self.on_server(self.server.backend.notice_departures), [])
        self.not_told(alice)
        # And back: the first packet makes bob present again, and the next look says so.
        unmute()
        bob.secure.ping()
        self.assertEqual(self.on_server(self.server.backend.notice_departures), [])
        self.assertEqual(self.told(alice), change(bob))
        self.assertTrue(self.logged("bob is here: told alice"))
        self.assertEqual(alice.relationships(), {"bob": (model.FRIEND, True)})

    def test_going_quiet_without_friends_tells_nobody(self):
        alice = self.player("alice")
        bob = self.player("bob")
        silence(self, bob)
        self.assertEqual([c.name for c in self.on_server(self.server.backend.notice_departures)],
                         ["bob"])
        self.not_told(alice)

    def test_registering_again_is_not_signing_in(self):
        """The same game registering its addresses again tells nobody anything new."""
        alice = self.player("alice")
        bob = self.player("bob")
        self.befriend(alice, bob)
        bob.register()
        self.not_told(alice, bob)

    def test_a_blocked_friend_is_not_told(self):
        alice = self.player("alice")
        bob = self.player("bob")
        self.befriend(alice, bob)
        alice.call(F, "BlackList", {"uiPlayer": bob.pid})
        self.drain(bob)
        self.player("alice")
        self.not_told(bob)


class UplayTab(Social):
    def setUp(self):
        super().setUp()
        self.alice = self.player("alice")
        self.bob = self.player("bob")

    def info(self, player):
        return {"to": {"m_principalId": player.pid, "m_ubiAccountId": player.name}}

    def test_the_whole_dance_from_the_uplay_side(self):
        self.assertIsNone(self.alice.call(U, "RequestFriendship", self.info(self.bob)).get("retval"))
        self.assertEqual(self.bob.relationships(), {"alice": (model.REQUEST_RECEIVED, True)})
        self.assertEqual(self.told(self.bob), change(self.alice, model.NEW_REQUEST))
        self.bob.call(U, "AcceptFriendship", self.info(self.alice))
        self.assertEqual(self.alice.relationships(), {"bob": (model.FRIEND, True)})
        self.assertEqual(self.told(self.alice), change(self.bob))

        rows = self.alice.call(U, "GetRelationshipsList")["relationshipsList"]
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["m_from"], {"m_principalId": self.alice.pid, "m_ubiAccountId": "alice"})
        self.assertEqual(row["m_to"], {"m_principalId": self.bob.pid, "m_ubiAccountId": "bob"})
        self.assertEqual(row["m_relationship"], model.FRIEND)
        self.assertFalse(row["m_onBlacklist"])
        self.assertGreater(row["m_friendsDate"], 0)

        names = self.alice.call(A, "LookupUsernamesByUbiAccountIDs",
                                {"UbiAccountIds": [row["m_to"]["m_ubiAccountId"], "ghost"]})
        self.assertEqual(dict(names["Usernames"]), {"bob": "bob"})

        self.bob.call(U, "ClearRelationship", self.info(self.alice))
        self.assertEqual(self.alice.relationships(), {})
        self.assertEqual(self.told(self.alice), change(self.bob))

    def test_it_lists_what_get_relationships_lists(self):
        carol = self.player("carol")
        self.befriend(self.alice, self.bob)
        self.ask(self.alice, carol)
        self.assertEqual(self.uplay(self.alice), [("bob", model.FRIEND, False),
                                                  ("carol", model.REQUEST_SENT, False)])

    def test_by_account_id_alone(self):
        self.alice.call(U, "RequestFriendship", {"to": {"m_principalId": 0,
                                                        "m_ubiAccountId": "BOB"}})
        self.bob.call(U, "DeclineFriendship", {"to": {"m_principalId": 0,
                                                      "m_ubiAccountId": "alice"}})
        self.assertEqual(self.alice.relationships(), {})

    def test_blocking_from_the_uplay_side(self):
        self.alice.call(U, "AddToBlacklist", self.info(self.bob))
        self.assertFalse(self.bob.call(F, "AddFriendByName",
                                       {"strPlayerName": "alice"})["retval"])

    def test_nobody_is_not_found(self):
        with self.assertRaises(RmcFailure) as caught:
            self.alice.call(U, "RequestFriendship", {"to": {"m_principalId": 0x7777,
                                                            "m_ubiAccountId": "ghost"}})
        self.assertEqual(caught.exception.code, Errors.FRIENDS_USER_NOT_FOUND)
        with self.assertRaises(RmcFailure) as caught:
            self.alice.call(U, "AddToBlacklist", {"to": {"m_principalId": 0,
                                                         "m_ubiAccountId": ""}})
        self.assertEqual(caught.exception.code, Errors.FRIENDS_USER_NOT_FOUND)

    def test_the_other_lookups(self):
        self.assertEqual(dict(self.alice.call(A, "LookupUbiAccountIDsByUsernames", {
            "Usernames": ["bob", "ghost"]})["UbiAccountIDs"]), {"bob": "bob"})
        self.assertEqual(dict(self.alice.call(A, "LookupUbiAccountIDsByPids", {
            "pids": [self.bob.pid, 0x7777]})["ubiaccountIDs"]), {self.bob.pid: "bob"})
        self.assertEqual(dict(self.alice.call(A, "LookupPrincipalIds", {
            "ubiAccountIds": ["bob", "ghost"]})["pids"]), {"bob": self.bob.pid})
        self.assertEqual(dict(self.alice.call(A, "LookupUbiAccountIDsByUsernameSubString", {
            "UsernameSubString": "o"})["UbiAccountIDs"]), {"bob": "bob"})
        self.assertEqual(dict(self.alice.call(A, "UserHasPlayed", {
            "UbiAccountIDs": ["bob", "ghost"]})["UserPresence"]), {"bob": True, "ghost": False})
        silence(self, self.bob)
        self.assertEqual(dict(self.alice.call(A, "IsUserPlaying", {
            "UbiAccountIDs": ["alice", "bob"]})["UserPresence"]), {"alice": True, "bob": False})


class InvitationMessages(Social):
    def setUp(self):
        super().setUp()
        self.alice = self.player("alice")
        self.bob = self.player("bob")

    def test_a_message_makes_a_request_and_can_be_dismissed(self):
        self.alice.call(M, "SendFriendInvitationMessage", {"receiptPid": self.bob.pid,
                                                           "messageBody": "hi"})
        self.assertEqual(self.bob.relationships(), {"alice": (model.REQUEST_RECEIVED, True)})
        self.assertEqual(self.bob.call(M, "ReceiveFriendInvitationMessages")["messages"],
                         [{"m_senderPid": self.alice.pid, "m_message": "hi"}])
        self.assertEqual(self.told(self.bob), change(self.alice, model.NEW_REQUEST))
        self.bob.call(M, "RemoveFriendInvitationMessage", {"senderPid": self.alice.pid})
        # The text goes, the request stays, and the sender's lists did not change.
        self.assertEqual(self.bob.call(M, "ReceiveFriendInvitationMessages")["messages"],
                         [{"m_senderPid": self.alice.pid, "m_message": ""}])
        self.assertEqual(self.bob.relationships(), {"alice": (model.REQUEST_RECEIVED, True)})
        self.not_told(self.alice)

    def test_the_game_sends_the_request_then_the_text(self):
        """The add-friend dialog: AddFriendByName with no text, then the text on its own
        (DLL 1041D1D9..1041D21D). The text arriving is news too."""
        self.ask(self.alice, self.bob, "")
        self.assertEqual(self.told(self.bob), change(self.alice, model.NEW_REQUEST))
        self.alice.call(M, "SendFriendInvitationMessage", {"receiptPid": self.bob.pid,
                                                           "messageBody": "co-op tonight?"})
        self.assertEqual(self.told(self.bob), change(self.alice))
        self.assertEqual(self.bob.call(M, "ReceiveFriendInvitationMessages")["messages"],
                         [{"m_senderPid": self.alice.pid, "m_message": "co-op tonight?"}])
        # Accepting dismisses the text with it, as the game's own two calls do.
        self.bob.call(F, "AcceptFriendship", {"uiPlayer": self.alice.pid})
        self.bob.call(M, "RemoveFriendInvitationMessage", {"senderPid": self.alice.pid})
        self.assertEqual(self.bob.call(M, "ReceiveFriendInvitationMessages")["messages"], [])

    def test_to_somebody_who_blocked_you(self):
        """Answered like any other - the method has no result - but it is no request."""
        self.bob.call(F, "BlackList", {"uiPlayer": self.alice.pid})
        self.assertEqual(self.alice.call(M, "SendFriendInvitationMessage", {
            "receiptPid": self.bob.pid, "messageBody": "please"}), {})
        self.assertEqual(self.bob.call(M, "ReceiveFriendInvitationMessages")["messages"], [])
        self.assertEqual(self.bob.relationships(), {})
        self.assertEqual(self.blocklist(self.bob), ["alice"])
        self.not_told(self.alice, self.bob)
        self.assertTrue(self.logged("alice's request to bob went nowhere"))

    def test_to_nobody(self):
        with self.assertRaises(RmcFailure) as caught:
            self.alice.call(M, "SendFriendInvitationMessage", {"receiptPid": 0x7777,
                                                               "messageBody": "hi"})
        self.assertEqual(caught.exception.code, Errors.FRIENDS_USER_NOT_FOUND)


def presence(*arguments) -> bytes:
    """A presence blob as TaskRDV_SetPresence (912C00) builds one: a u32 count, then per argument
    a u32 id, a u32 tag and the value - all of it sent as "%02x" text. Values are given as the
    bytes the tag calls for."""
    data = struct.pack("<I", len(arguments))
    for argument_id, tag, value in arguments:
        data += struct.pack("<II", argument_id, tag) + value
    return data.hex().encode("ascii")


LOBBY = presence((1, 1, struct.pack("<i", 3)), (2, 5, struct.pack("<I", 5) + b"lobby"))


class PresenceBlobs(unittest.TestCase):
    """What GetPresence's reader (913190 -> 8B6950) can walk without leaving its buffers."""

    def test_what_the_game_builds_is_readable(self):
        for blob in (b"", presence(), LOBBY,
                     presence((1, 2, bytes(8)), (2, 3, bytes(4)), (3, 4, bytes(8)), (4, 6, b""),
                              (5, 99, b""))):
            self.assertTrue(readable(blob), blob)
        # The most the reader takes: 255 bytes, 510 characters.
        biggest = presence((1, 5, struct.pack("<I", 255 - 16) + b"x" * (255 - 16)))
        self.assertEqual(len(biggest), 510)
        self.assertTrue(readable(biggest))
        self.assertTrue(readable(LOBBY.upper()), "sscanf %2x reads either case")

    def test_what_would_take_the_reader_off_its_buffers_is_not(self):
        too_long = presence((1, 5, struct.pack("<I", 256 - 16) + b"x" * (256 - 16)))
        for blob in (too_long, LOBBY[:-1], b"zz" + LOBBY[2:], b"\x01\x02lobby", b"0100",
                     presence()[:-2] + b"05",                         # a count with nothing
                     presence((1, 5, struct.pack("<I", 40) + b"short")),
                     presence((1, 2, bytes(4))),                     # 8 bytes promised, 4 sent
                     presence((1, 5, b"\x01"))):                     # a length cut short
            self.assertFalse(readable(blob), blob)


class Presence(Social):
    """RichPresenceProtocol: what FIND FRIENDS shows about the players it found."""

    def setUp(self):
        super().setUp()
        self.alice = self.player("alice")
        self.bob = self.player("bob")

    def get(self, who, *pids):
        return [(r["m_pid"], r["m_isOnline"], r["m_presenceId"], r["m_presence"])
                for r in who.call(R, "GetPresence", {"pids": list(pids)})["presences"]]

    def test_what_was_set_is_what_others_get(self):
        self.assertEqual(self.bob.call(R, "SetPresence", {"presenceId": 7,
                                                          "presence": LOBBY}), {})
        self.assertEqual(self.get(self.alice, self.bob.pid, self.alice.pid),
                         [(self.bob.pid, True, 7, LOBBY),
                          (self.alice.pid, True, 0, b"")])
        self.bob.call(R, "SetPresence", {"presenceId": 8, "presence": b""})
        self.assertEqual(self.get(self.alice, self.bob.pid), [(self.bob.pid, True, 8, b"")])

    def test_the_wire_layout(self):
        """{u32, bool as one byte, u32, qBuffer} per entry, after a u32 count (91E7C0)."""
        self.bob.call(R, "SetPresence", {"presenceId": 0x01020304, "presence": presence()})
        reply = self.alice.call_raw(R, 2, bytes.fromhex("01000000") +
                                    self.bob.pid.to_bytes(4, "little"))
        self.assertTrue(reply.success)
        self.assertEqual(reply.body, bytes.fromhex("01000000") + self.bob.pid.to_bytes(4, "little")
                         + bytes.fromhex("01 04030201 0800") + b"00000000")

    def test_a_blob_no_game_could_read_is_not_handed_on(self):
        """The reader copies the blob onto its stack and walks it without bounds, so a bad one
        would crash whoever found this player. The presence id still counts."""
        for blob in (b"\x01\x02lobby", b"0" * 512, presence((1, 5, struct.pack("<I", 99)))):
            self.bob.call(R, "SetPresence", {"presenceId": 7, "presence": blob})
            self.assertEqual(self.get(self.alice, self.bob.pid), [(self.bob.pid, True, 7, b"")])
        self.assertEqual(self.count_logged("set a presence blob no game could read back"), 3)

    def test_nobody_and_nothing(self):
        self.assertEqual(self.get(self.alice, 0x7777, 0), [(0x7777, False, 0, b""),
                                                           (0, False, 0, b"")])
        self.assertEqual(self.get(self.alice), [])

    def test_somebody_who_went_quiet_has_no_presence(self):
        self.bob.call(R, "SetPresence", {"presenceId": 7, "presence": LOBBY})
        silence(self, self.bob)
        self.assertEqual(self.get(self.alice, self.bob.pid), [(self.bob.pid, False, 0, b"")])

    def test_a_restarted_game_starts_without_one(self):
        self.bob.call(R, "SetPresence", {"presenceId": 7, "presence": LOBBY})
        again = self.player("bob")
        self.assertEqual(self.get(self.alice, again.pid), [(again.pid, True, 0, b"")])
        again.call(R, "SetPresence", {"presenceId": 9, "presence": presence()})
        self.assertEqual(self.get(self.alice, again.pid), [(again.pid, True, 9, b"00000000")])


class EveryoneIsAFriend(Social):
    settings = {"auto_friends": "yes"}

    def test_they_see_each_other_without_asking(self):
        alice = self.player("alice")
        bob = self.player("bob")
        carol = self.player("carol")
        self.assertEqual(alice.relationships(), {"bob": (model.FRIEND, True),
                                                 "carol": (model.FRIEND, True)})
        rows = bob.call(U, "GetRelationshipsList")["relationshipsList"]
        self.assertEqual(sorted(r["m_to"]["m_ubiAccountId"] for r in rows), ["alice", "carol"])
        self.assertEqual(self.blocklist(alice), [])

    def test_asking_and_removing_change_nothing(self):
        alice = self.player("alice")
        bob = self.player("bob")
        self.drain(alice, bob)
        self.assertTrue(self.ask(alice, bob))
        self.assertTrue(bob.call(F, "AcceptFriendship", {"uiPlayer": alice.pid})["retval"])
        self.assertFalse(bob.call(F, "DeclineFriendship", {"uiPlayer": alice.pid})["retval"])
        self.assertFalse(alice.call(F, "ClearRelationship", {"uiPlayer": bob.pid})["retval"])
        self.assertEqual(bob.relationships(), {"alice": (model.FRIEND, True)})
        self.not_told(alice, bob)

    def test_blocking_still_hides_somebody_both_ways(self):
        alice = self.player("alice")
        bob = self.player("bob")
        carol = self.player("carol")
        self.drain(alice, bob, carol)
        alice.call(F, "BlackList", {"uiPlayer": carol.pid})
        self.assertEqual(list(alice.relationships()), ["bob"])
        self.assertEqual(list(carol.relationships()), ["bob"])
        self.assertEqual(self.blocklist(alice), ["carol"])
        self.assertEqual(self.told(carol), change(alice))
        self.assertFalse(self.ask(carol, alice))
        # The game's Unblock brings them back as friends straight away in this mode.
        self.assertTrue(self.ask(alice, carol, ""))
        self.assertEqual(self.blocklist(alice), [])
        self.assertEqual(carol.relationships()["alice"], (model.FRIEND, True))
        self.assertEqual(self.told(carol), change(alice))

    def test_signing_in_tells_everyone(self):
        alice = self.player("alice")
        bob = self.player("bob")
        self.assertEqual(self.told(alice), change(bob))
        self.not_told(bob)
