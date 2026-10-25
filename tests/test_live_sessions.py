"""Co-op and the squad against a running server: hosting, searching, joining, inviting, leaving
and migrating, with two and three games at once."""

import queue
import shutil
import tempfile
import time
import unittest

from grfs import sessions as model
from grfs.backend import FORGET_AFTER, PRESENCE_WINDOW
from grfs.fakegame import RmcFailure
from grfs.harness import Harness
from grfs.services.game_session import INVITATION_ACCEPTED, INVITATION_RECEIVED, Errors

from tests.live import LiveTest

G = "GameSessionProtocol"
TYPE = 1

# What the game's custom-match create sends, near enough: capacities, the "not invite-only" flag
# the client prepends, the used counts it always sends as 0, and its own game attributes.
MATCH = {3: 4, 4: 0, 5: 0, 6: 0, 7: 0, 0x64: 1, 0x65: 2, 0x67: 0, 0x69: 77, 0x70: 3, 0x71: 0,
         0x72: 5, 0x73: 0, 0x74: 2, 0x75: 0}


def props(values: dict) -> list:
    return [{"m_ID": k, "m_value": v} for k, v in values.items()]


def as_dict(properties: list) -> dict:
    return {p["m_ID"]: p["m_value"] for p in properties}


def key(session_id, type_id=TYPE):
    return {"m_typeID": type_id, "m_sessionID": session_id}


class Sessions(LiveTest):
    def setUp(self):
        super().setUp()
        self.alice = self.player("alice")
        self.bob = self.player("bob")
        self.carol = self.player("carol")

    # ----- the calls, as the game makes them -------------------------------------------------
    def host(self, player, attributes=None, add_self=True, changes=None) -> dict:
        values = dict(MATCH if attributes is None else attributes)
        values.update(changes or {})
        k = player.call(G, "CreateSession", {"gameSession": {
            "m_typeID": TYPE, "m_attributes": props(values)}})["gameSessionKey"]
        if add_self:
            self.add(player, k, [player])
        return k

    def add(self, host, k, public=(), private=()):
        host.call(G, "AddParticipants", {"gameSessionKey": k,
                                         "publicParticipantIDs": [p.pid for p in public],
                                         "privateParticipantIDs": [p.pid for p in private]})

    def get(self, who, k) -> dict:
        return who.call(G, "GetSession", {"gameSessionKey": k})["searchResult"]

    def attributes(self, who, k) -> dict:
        return as_dict(self.get(who, k)["m_attributes"])

    def search(self, who, query_id=2, **params) -> list:
        values = {0x64: 1, 0x65: 2, 0x69: 77}
        values.update({int(k[1:], 16): v for k, v in params.items()})
        return who.call(G, "SearchSessions", {"gameSessionQuery": {
            "m_typeID": TYPE, "m_queryID": query_id,
            "m_parameters": props({k: v for k, v in values.items() if v is not None})}}
        )["searchResults"]

    def found(self, who, query_id=2, **params) -> list:
        return [r["m_sessionKey"]["m_sessionID"] for r in self.search(who, query_id, **params)]

    def participants(self, who, k) -> list:
        return [p["m_PID"] for p in who.call(G, "GetParticipants", {
            "gameSessionKey": k, "resultRange": {"m_uiOffset": 0, "m_uiSize": 100}}
        )["participants"]]

    def fails(self, code, who, method, args):
        with self.assertRaises(RmcFailure) as caught:
            who.call(G, method, args)
        self.assertEqual(caught.exception.code, code, "0x%08X" % caught.exception.code)

    def notification(self, player, event_type, timeout=3.0) -> dict:
        """The next notification of this type the server sent the player; others are skipped."""
        deadline = time.monotonic() + timeout
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                self.fail("%s was never sent notification %d" % (player.name, event_type))
            try:
                protocol, method, args = player.next_server_call(timeout=left)
            except queue.Empty:
                continue
            if protocol == "NotificationProtocol" and args["oEvent"]["m_uiType"] == event_type:
                return args["oEvent"]

    def no_notification(self, player, event_type, wait=0.3):
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            try:
                _, _, args = player.next_server_call(timeout=max(0.01, deadline - time.monotonic()))
            except queue.Empty:
                return
            self.assertNotEqual(args.get("oEvent", {}).get("m_uiType"), event_type)


class Hosting(Sessions):
    def test_the_server_owns_the_used_slots(self):
        k = self.host(self.alice, add_self=False, changes={5: 3, 6: 2, 0x32: 9, 0x33: 9, 0: 2})
        self.assertEqual(k["m_typeID"], TYPE)
        self.assertGreater(k["m_sessionID"], 0)
        values = self.attributes(self.bob, k)
        self.assertEqual((values[3], values[4], values[5], values[6]), (4, 0, 0, 0))
        for absent in (0, 0x32, 0x33):
            self.assertNotIn(absent, values)
        self.assertEqual(values[7], 0)
        self.assertEqual(values[0x70], 3)
        self.add(self.alice, k, [self.alice])
        self.assertEqual(self.attributes(self.bob, k)[5], 1)

    def test_every_result_carries_3_to_6(self):
        k = self.host(self.alice, {0x64: 1}, add_self=False)
        values = self.attributes(self.bob, k)
        self.assertEqual([values.get(a) for a in (3, 4, 5, 6)], [0, 0, 0, 0])

    def test_sessions_get_their_own_ids(self):
        first = self.host(self.alice)
        second = self.host(self.bob)
        self.assertNotEqual(first["m_sessionID"], second["m_sessionID"])

    def test_an_update_is_merged_not_replaced(self):
        k = self.host(self.alice)
        self.alice.call(G, "UpdateSession", {"gameSessionUpdate": {
            "m_sessionKey": k, "m_attributes": props({0x70: 9})}})
        values = self.attributes(self.bob, k)
        self.assertEqual(values[0x70], 9)
        self.assertEqual((values[3], values[4], values[5], values[0x64], values[0x72]),
                         (4, 0, 1, 1, 5))
        # What the server owns is not the client's to set, and the rest of the update still is.
        self.alice.call(G, "UpdateSession", {"gameSessionUpdate": {
            "m_sessionKey": k, "m_attributes": props({5: 4, 6: 1, 0: 2, 0x32: 0, 0x33: 0,
                                                      3: 6, 0x66: 2})}})
        values = self.attributes(self.bob, k)
        self.assertEqual((values[3], values[5], values[6], values[0x66]), (6, 1, 0, 2))
        self.assertNotIn(0, values)
        self.assertNotIn(0x32, values)

    def test_only_the_host_updates_adds_or_removes(self):
        k = self.host(self.alice)
        update = {"gameSessionUpdate": {"m_sessionKey": k, "m_attributes": props({0x70: 1})}}
        self.fails(Errors.SESSION_HOST_PRIVILEGE_REQUIRED, self.bob, "UpdateSession", update)
        self.fails(Errors.SESSION_HOST_PRIVILEGE_REQUIRED, self.bob, "AddParticipants", {
            "gameSessionKey": k, "publicParticipantIDs": [self.bob.pid]})
        self.fails(Errors.SESSION_HOST_PRIVILEGE_REQUIRED, self.bob, "RemoveParticipants", {
            "gameSessionKey": k, "participantIDs": [self.alice.pid]})
        self.fails(Errors.SESSION_HOST_PRIVILEGE_REQUIRED, self.bob, "DeleteSession",
                   {"gameSessionKey": k})
        self.assertEqual(self.attributes(self.bob, k)[0x70], 3)

    def test_a_session_that_is_not_there(self):
        for method, args in (("GetSession", {"gameSessionKey": key(999)}),
                             ("UpdateSession", {"gameSessionUpdate": {"m_sessionKey": key(999)}}),
                             ("JoinSession", {"gameSessionKey": key(999)}),
                             ("LeaveSession", {"gameSessionKey": key(999)}),
                             ("MigrateSession", {"gameSessionKey": key(999)}),
                             ("AddParticipants", {"gameSessionKey": key(999)}),
                             ("GetParticipants", {"gameSessionKey": key(999)}),
                             ("SendInvitation", {"invitation": {"m_sessionKey": key(999),
                                                                "m_recipientPIDs": [1]}})):
            self.fails(Errors.SESSION_INVALID_SESSION_KEY, self.alice, method, args)

    def test_found_by_id_whatever_the_type_says(self):
        # The key that comes with an invitation was built from a notification and has no type.
        k = self.host(self.alice)
        result = self.get(self.bob, key(k["m_sessionID"], 0xDEADBEEF))
        self.assertEqual(result["m_sessionKey"], k)

    def test_host_urls_are_the_ones_registered_for_sessions(self):
        k = self.host(self.alice)
        self.assertEqual(self.get(self.bob, k)["m_hostURLs"], self.client_of(self.alice).urls)
        self.assertTrue(self.get(self.bob, k)["m_hostURLs"])
        mine = ["prudp:/address=10.1.2.3;port=3074;sid=15;type=3",
                "prudp:/address=192.168.0.7;port=3074;sid=15;type=2"]
        self.alice.call(G, "RegisterURLs", {"stationURLs": mine})
        result = self.get(self.bob, k)
        self.assertEqual(result["m_hostURLs"], mine)
        self.assertEqual(result["m_hostPID"], self.alice.pid)

    def test_deleting(self):
        k = self.host(self.alice)
        self.alice.call(G, "SendInvitation", {"invitation": {
            "m_sessionKey": k, "m_recipientPIDs": [self.bob.pid], "m_message": "x"}})
        self.alice.call(G, "DeleteSession", {"gameSessionKey": k})
        self.fails(Errors.SESSION_INVALID_SESSION_KEY, self.bob, "GetSession",
                   {"gameSessionKey": k})
        self.assertEqual(self.bob.call(G, "GetInvitationsReceived", {
            "gameSessionTypeID": TYPE, "resultRange": {"m_uiSize": 25}})["invitations"], [])


class Searching(Sessions):
    def test_host_search_join_and_add(self):
        k = self.host(self.alice)
        sid = k["m_sessionID"]
        # Nobody is offered their own session.
        self.assertEqual(self.found(self.alice), [])
        results = self.search(self.bob, x74=2, x72=5)
        self.assertEqual(len(results), 1)
        result = results[0]
        self.assertEqual(result["m_sessionKey"], k)
        self.assertEqual(result["m_hostPID"], self.alice.pid)
        self.assertEqual(result["m_hostURLs"], self.client_of(self.alice).urls)
        self.assertEqual(as_dict(result["m_attributes"])[5], 1)
        self.assertEqual(as_dict(result["m_attributes"])[0x70], 3)

        # Joining asks for nothing to be counted; the host's AddParticipants does that.
        self.bob.call(G, "JoinSession", {"gameSessionKey": k})
        self.assertEqual(self.attributes(self.carol, k)[5], 1)
        self.add(self.alice, k, [self.bob])
        self.assertEqual(self.attributes(self.carol, k)[5], 2)
        self.assertEqual(self.found(self.bob), [])
        self.assertEqual(self.found(self.carol), [sid])
        self.assertEqual(self.participants(self.carol, k), [self.alice.pid, self.bob.pid])

    def test_what_a_search_leaves_out(self):
        full = self.host(self.alice, changes={3: 2})
        self.add(self.alice, full, [self.bob])
        squad = self.host(self.bob, changes={3: 4, 4: 4, 7: 1})
        other_build = self.host(self.carol, changes={0x69: 78})
        other_type = self.alice.call(G, "CreateSession", {"gameSession": {
            "m_typeID": 2, "m_attributes": props(MATCH)}})["gameSessionKey"]
        dave = self.player("dave")
        for k in (full, squad, other_build, other_type):
            self.assertNotIn(k["m_sessionID"], self.found(dave))
        self.assertEqual(self.found(dave), [])
        # Room again: found again.
        self.alice.call(G, "RemoveParticipants", {"gameSessionKey": full,
                                                  "participantIDs": [self.bob.pid]})
        self.assertEqual(self.found(dave), [full["m_sessionID"]])

    def test_parameters_match_only_what_the_session_has(self):
        k = self.host(self.alice, {3: 4, 4: 0, 0x64: 1, 0x65: 2, 0x69: 77})
        sid = k["m_sessionID"]
        # 0x74 and 0x72 are not on the session, so they do not rule it out.
        self.assertEqual(self.found(self.bob, x74=5, x72=1), [sid])
        self.assertEqual(self.found(self.bob, x65=3), [])
        # The quick-match extras never match anything on the host's side and are not compared.
        self.assertEqual(self.found(self.bob, x6c=1234, x6e=5, x6f=0), [sid])

    def test_a_party_has_to_fit(self):
        k = self.host(self.alice)               # 4 public slots, 1 used
        sid = k["m_sessionID"]
        self.assertEqual(self.found(self.bob, x3=3), [sid])
        self.assertEqual(self.found(self.bob, x3=4), [])
        self.add(self.alice, k, [self.carol])
        self.assertEqual(self.found(self.bob, x3=3), [])
        self.assertEqual(self.found(self.bob, x3=2), [sid])

    def test_quick_match_finds_the_lobby_size(self):
        # The quick-match host doubles the lobby size n into its capacity: 3 = 2n, 4 = 0.
        k = self.host(self.alice, {3: 8, 4: 0, 0x64: 1, 0x65: 3, 0x69: 77, 0x6a: 0, 0x6b: 2,
                                   0x6c: 0})
        sid = k["m_sessionID"]
        quick = dict(query_id=3, x65=3, x6e=5, x6f=0)
        self.assertEqual(self.found(self.bob, **quick, x3=4, x6c=0), [sid])
        self.assertEqual(self.found(self.bob, **quick, x3=4, x6c=4242), [sid])
        self.assertEqual(self.found(self.bob, **quick, x3=6), [])
        self.assertEqual(self.found(self.bob, **quick, x3=2), [])
        # More than half full and still offered: n is the lobby size, not a head count.
        others = [self.player("p%d" % i) for i in range(5)]
        self.add(self.alice, k, others)
        self.assertEqual(self.attributes(self.bob, k)[5], 6)
        self.assertEqual(self.found(self.bob, **quick, x3=4), [sid])
        self.add(self.alice, k, [self.carol, self.player("p5")])
        self.assertEqual(self.found(self.bob, **quick, x3=4), [])

    def test_only_two_queries_exist(self):
        for query_id in (0, 1, 4):
            self.fails(Errors.SESSION_INVALID_QUERY_ID, self.bob, "SearchSessions",
                       {"gameSessionQuery": {"m_typeID": TYPE, "m_queryID": query_id}})

    def test_at_most_twenty_newest_first(self):
        made = [self.host(self.alice)["m_sessionID"] for _ in range(model.SEARCH_LIMIT + 3)]
        found = self.found(self.bob)
        self.assertEqual(found, sorted(made, reverse=True)[:model.SEARCH_LIMIT])

    def test_no_host_no_offer(self):
        k = self.host(self.alice)
        self.add(self.alice, k, [self.bob])
        self.alice.call(G, "AbandonSession", {"gameSessionKey": k})
        self.assertEqual(self.found(self.carol), [])
        other = self.host(self.bob)
        self.assertEqual(self.found(self.carol), [other["m_sessionID"]])

        def quiet():
            self.server.backend.clients.by_pid(self.bob.pid).last_seen -= PRESENCE_WINDOW + 1
        self.on_server(quiet)
        self.assertEqual(self.found(self.carol), [])


class Slots(Sessions):
    def test_counts_follow_the_lists(self):
        k = self.host(self.alice, changes={3: 2, 4: 2})
        self.add(self.alice, k, [self.alice, self.bob], [self.carol])
        values = self.attributes(self.bob, k)
        self.assertEqual((values[5], values[6]), (2, 1))
        self.assertEqual(self.bob.call(G, "GetParticipantCount", {"gameSessionKey": k})["count"],
                         3)
        self.alice.call(G, "RemoveParticipants", {"gameSessionKey": k,
                                                  "participantIDs": [self.carol.pid, 0x7777]})
        values = self.attributes(self.bob, k)
        self.assertEqual((values[5], values[6]), (2, 0))

    def test_never_refused_and_never_past_capacity(self):
        k = self.host(self.alice, changes={3: 2, 4: 0})
        dave = self.player("dave")
        self.add(self.alice, k, [self.bob, self.carol, dave])
        self.assertEqual(len(self.participants(self.bob, k)), 4)
        values = self.attributes(self.bob, k)
        self.assertEqual((values[3], values[5]), (2, 2))

    def test_private_members_without_private_slots_take_public_ones(self):
        # Both match creates send 4 = 0, and the host still lists an invited friend as private.
        k = self.host(self.alice, changes={3: 4, 4: 0})
        self.add(self.alice, k, private=[self.bob])
        values = self.attributes(self.carol, k)
        self.assertEqual((values[5], values[6]), (2, 0))
        self.assertEqual(self.participants(self.carol, k), [self.alice.pid, self.bob.pid])

    def test_a_member_is_listed_once(self):
        k = self.host(self.alice, changes={3: 4, 4: 4})
        self.add(self.alice, k, [self.bob], [self.bob, self.alice])
        self.add(self.alice, k, [self.bob])
        self.assertEqual(self.participants(self.carol, k), [self.alice.pid, self.bob.pid])
        values = self.attributes(self.carol, k)
        self.assertEqual((values[5], values[6]), (2, 0))

    def test_joining_a_full_session(self):
        k = self.host(self.alice, changes={3: 2})
        self.add(self.alice, k, [self.bob])
        self.fails(Errors.SESSION_NO_PUBLIC_SLOT, self.carol, "JoinSession", {"gameSessionKey": k})
        # Somebody already in it is not turned away.
        self.bob.call(G, "JoinSession", {"gameSessionKey": k})

    def test_participants_with_their_names_and_urls(self):
        k = self.host(self.alice)
        self.add(self.alice, k, [self.bob])
        bob_urls = ["prudp:/address=10.0.0.2;port=3074;sid=15;type=2"]
        self.bob.call(G, "RegisterURLs", {"stationURLs": bob_urls})
        rows = self.carol.call(G, "GetParticipants", {
            "gameSessionKey": k, "resultRange": {"m_uiOffset": 0, "m_uiSize": 100}}
        )["participants"]
        self.assertEqual([(r["m_PID"], r["m_name"]) for r in rows],
                         [(self.alice.pid, "alice"), (self.bob.pid, "bob")])
        self.assertEqual(rows[0]["m_stationURLs"], self.client_of(self.alice).urls)
        self.assertEqual(rows[1]["m_stationURLs"], bob_urls)
        page = self.carol.call(G, "GetParticipants", {
            "gameSessionKey": k, "resultRange": {"m_uiOffset": 1, "m_uiSize": 1}})["participants"]
        self.assertEqual([r["m_PID"] for r in page], [self.bob.pid])
        urls = self.carol.call(G, "GetParticipantsURLs", {
            "gameSessionKey": k, "participantIDs": [self.bob.pid, self.carol.pid]}
        )["participants"]
        self.assertEqual([(r["m_PID"], r["m_stationURLs"]) for r in urls],
                         [(self.bob.pid, bob_urls)])


class LeavingAndMigrating(Sessions):
    def test_a_member_leaves(self):
        k = self.host(self.alice)
        self.add(self.alice, k, [self.bob])
        self.bob.call(G, "LeaveSession", {"gameSessionKey": k})
        self.assertEqual(self.attributes(self.carol, k)[5], 1)
        self.assertEqual(self.participants(self.carol, k), [self.alice.pid])

    def test_the_host_abandons_and_a_member_takes_over(self):
        k = self.host(self.alice, changes={3: 4, 4: 2})
        self.add(self.alice, k, [self.bob], [self.carol])
        self.alice.call(G, "AbandonSession", {"gameSessionKey": k})
        left = self.get(self.bob, k)
        self.assertEqual(left["m_hostPID"], 0)
        self.assertEqual(self.participants(self.bob, k), [self.bob.pid, self.carol.pid])

        migrated = self.bob.call(G, "MigrateSession", {"gameSessionKey": k})
        self.assertEqual(migrated["gameSessionKeyMigrated"], k)
        result = self.get(self.carol, k)
        self.assertEqual(result["m_hostPID"], self.bob.pid)
        self.assertEqual(result["m_hostURLs"], self.client_of(self.bob).urls)
        values = as_dict(result["m_attributes"])
        self.assertEqual((values[3], values[4], values[5], values[6], values[0x70]),
                         (4, 2, 1, 1, 3))
        # The new host brings the list in line with its own, which it now may.
        self.assertEqual(self.participants(self.bob, k), [self.bob.pid, self.carol.pid])
        dave = self.player("dave")
        self.add(self.bob, k, [dave])
        self.bob.call(G, "UpdateSession", {"gameSessionUpdate": {
            "m_sessionKey": k, "m_attributes": props({0x70: 8})}})
        self.assertEqual(self.found(self.alice), [k["m_sessionID"]])
        self.fails(Errors.SESSION_HOST_PRIVILEGE_REQUIRED, self.alice, "UpdateSession", {
            "gameSessionUpdate": {"m_sessionKey": k, "m_attributes": props({0x70: 1})}})

    def test_the_last_one_out_ends_it(self):
        k = self.host(self.alice)
        self.add(self.alice, k, [self.bob])
        self.alice.call(G, "AbandonSession", {"gameSessionKey": k})
        self.bob.call(G, "LeaveSession", {"gameSessionKey": k})
        self.fails(Errors.SESSION_INVALID_SESSION_KEY, self.carol, "GetSession",
                   {"gameSessionKey": k})

    def test_a_host_alone_abandons(self):
        k = self.host(self.alice)
        self.alice.call(G, "AbandonSession", {"gameSessionKey": k})
        self.fails(Errors.SESSION_INVALID_SESSION_KEY, self.bob, "GetSession",
                   {"gameSessionKey": k})

    def test_a_host_who_leaves_instead_of_abandoning(self):
        k = self.host(self.alice)
        self.add(self.alice, k, [self.bob])
        self.alice.call(G, "LeaveSession", {"gameSessionKey": k})
        self.assertEqual(self.get(self.bob, k)["m_hostPID"], 0)
        self.bob.call(G, "MigrateSession", {"gameSessionKey": k})
        self.assertEqual(self.get(self.bob, k)["m_hostPID"], self.bob.pid)

    def test_migrating_while_the_host_still_looks_present(self):
        k = self.host(self.alice)
        self.add(self.alice, k, [self.bob])
        # A member may, with the host still listed and online: it may have crashed a moment ago.
        self.bob.call(G, "MigrateSession", {"gameSessionKey": k})
        self.assertEqual(self.get(self.carol, k)["m_hostPID"], self.bob.pid)
        # And so may a member it never got round to adding, before the server knows it is gone.
        migrated = self.carol.call(G, "MigrateSession", {"gameSessionKey": k})
        self.assertEqual(migrated["gameSessionKeyMigrated"], k)
        self.assertEqual(self.get(self.bob, k)["m_hostPID"], self.carol.pid)
        self.assertEqual(self.participants(self.carol, k), [self.alice.pid, self.bob.pid])
        self.fails(Errors.SESSION_INVALID_SESSION_KEY, self.carol, "MigrateSession",
                   {"gameSessionKey": key(k["m_sessionID"] + 100)})

    def test_a_crashed_hosts_member_who_was_never_added(self):
        k = self.host(self.alice)
        self.add(self.alice, k, [self.bob])
        self.alice.call(G, "AbandonSession", {"gameSessionKey": k})
        # Carol had connected to the host, but it went before it added her.
        self.carol.call(G, "MigrateSession", {"gameSessionKey": k})
        self.assertEqual(self.get(self.bob, k)["m_hostPID"], self.carol.pid)
        self.add(self.carol, k, [self.carol])
        self.assertEqual(self.participants(self.bob, k), [self.bob.pid, self.carol.pid])


class Invitations(Sessions):
    def received(self, who) -> list:
        return who.call(G, "GetInvitationsReceived", {
            "gameSessionTypeID": TYPE, "resultRange": {"m_uiOffset": 0, "m_uiSize": 25}}
        )["invitations"]

    def invite(self, host, k, recipients, message="Do you want to play?"):
        host.call(G, "SendInvitation", {"invitation": {
            "m_sessionKey": k, "m_recipientPIDs": [p.pid for p in recipients],
            "m_message": message}})

    def test_invite_accept_and_join(self):
        k = self.host(self.alice)
        message = "Do you want to play Ghost Recon Future Soldier with me ? " + "x" * 30
        self.invite(self.alice, k, [self.bob], message)
        event = self.notification(self.bob, INVITATION_RECEIVED)
        self.assertEqual(event, {"m_pidSource": self.alice.pid, "m_uiType": 7005,
                                 "m_uiParam1": TYPE, "m_uiParam2": k["m_sessionID"],
                                 "m_strParam": message[:64], "m_uiParam3": 0})

        self.assertEqual(self.bob.call(G, "GetInvitationReceivedCount",
                                       {"gameSessionTypeID": TYPE})["count"], 1)
        got = self.received(self.bob)
        self.assertEqual(len(got), 1)
        self.assertEqual((got[0]["m_sessionKey"], got[0]["m_senderPID"], got[0]["m_message"]),
                         (k, self.alice.pid, message))
        self.assertGreater(got[0]["m_creationTime"], 0)
        sent = self.alice.call(G, "GetInvitationsSent", {"gameSessionTypeID": TYPE})
        self.assertEqual([i["m_recipientPID"] for i in sent["invitations"]], [self.bob.pid])
        self.assertEqual(self.alice.call(G, "GetInvitationSentCount",
                                         {"gameSessionTypeID": TYPE})["count"], 1)

        # The client's key here has no type: it was built from the notification.
        broken = dict(got[0], m_sessionKey=key(k["m_sessionID"], 0xCCCCCCCC))
        self.get(self.bob, broken["m_sessionKey"])
        self.bob.call(G, "AcceptInvitation", {"gameSessionInvitation": broken})
        accepted = self.notification(self.alice, INVITATION_ACCEPTED)
        self.assertEqual((accepted["m_pidSource"], accepted["m_uiParam2"]),
                         (self.bob.pid, k["m_sessionID"]))
        self.assertEqual(self.received(self.bob), [])
        self.assertEqual(self.alice.call(G, "GetInvitationsSent",
                                         {"gameSessionTypeID": TYPE})["invitations"], [])
        # Accepting counts nobody; the host adds the friend once they have connected.
        self.assertEqual(self.attributes(self.carol, k)[5], 1)
        self.add(self.alice, k, private=[self.bob])
        self.assertEqual(self.participants(self.carol, k), [self.alice.pid, self.bob.pid])

    def test_three_players_and_the_newest_last(self):
        k = self.host(self.alice)
        self.invite(self.alice, k, [self.bob, self.carol, self.bob])
        self.notification(self.bob, INVITATION_RECEIVED)
        self.notification(self.carol, INVITATION_RECEIVED)
        self.no_notification(self.bob, INVITATION_RECEIVED)
        theirs = self.host(self.carol)
        self.invite(self.carol, theirs, [self.bob], "mine instead")
        self.assertEqual([i["m_senderPID"] for i in self.received(self.bob)],
                         [self.alice.pid, self.carol.pid])
        # Asked again, alice's is the newest.
        self.invite(self.alice, k, [self.bob], "again")
        got = self.received(self.bob)
        self.assertEqual([(i["m_senderPID"], i["m_message"]) for i in got],
                         [(self.carol.pid, "mine instead"), (self.alice.pid, "again")])

        refused = self.received(self.carol)[0]
        self.carol.call(G, "DeclineInvitation", {"gameSessionInvitation": refused})
        self.assertEqual(self.received(self.carol), [])
        self.assertEqual(len(self.received(self.bob)), 2)

    def test_only_the_newest_twenty_five_are_kept(self):
        k = self.host(self.alice)
        for n in range(model.INVITATIONS_KEPT + 5):
            self.invite(self.alice, self.host(self.alice), [self.bob], "n%d" % n)
        self.invite(self.alice, k, [self.bob], "last")
        got = self.received(self.bob)
        self.assertEqual(len(got), model.INVITATIONS_KEPT)
        self.assertEqual(got[-1]["m_message"], "last")
        self.assertEqual(got[0]["m_message"], "n6")

    def test_taking_one_back(self):
        k = self.host(self.alice)
        self.invite(self.alice, k, [self.bob, self.carol])
        sent = self.alice.call(G, "GetInvitationsSent", {"gameSessionTypeID": TYPE})
        to_bob = next(i for i in sent["invitations"] if i["m_recipientPID"] == self.bob.pid)
        self.alice.call(G, "CancelInvitation", {"gameSessionInvitation": to_bob})
        self.assertEqual(self.received(self.bob), [])
        self.assertEqual(len(self.received(self.carol)), 1)

    def test_accepting_into_a_full_or_vanished_session(self):
        k = self.host(self.alice, changes={3: 2, 4: 0})
        self.invite(self.alice, k, [self.bob, self.carol])
        self.add(self.alice, k, [self.player("dave")])
        invitation = self.received(self.bob)[0]
        self.fails(Errors.SESSION_NO_PUBLIC_SLOT, self.bob, "AcceptInvitation",
                   {"gameSessionInvitation": invitation})
        self.assertEqual(self.received(self.bob), [])

        squad = self.host(self.alice, {3: 0, 4: 1, 7: 1})
        self.invite(self.alice, squad, [self.bob])
        self.add(self.alice, squad, private=[self.player("erin")])
        self.fails(Errors.SESSION_NO_PRIVATE_SLOT, self.bob, "AcceptInvitation",
                   {"gameSessionInvitation": self.received(self.bob)[0]})

        # A session that has gone takes its invitations with it, and accepting one is refused.
        gone = self.host(self.alice)
        self.invite(self.alice, gone, [self.carol])
        invitation = next(i for i in self.received(self.carol) if i["m_sessionKey"] == gone)
        self.alice.call(G, "AbandonSession", {"gameSessionKey": gone})
        self.assertNotIn(gone, [i["m_sessionKey"] for i in self.received(self.carol)])
        self.fails(Errors.SESSION_INVALID_SESSION_KEY, self.carol, "AcceptInvitation",
                   {"gameSessionInvitation": invitation})

    def test_a_squad_invitation_uses_the_private_slots(self):
        squad = self.host(self.alice, {3: 0, 4: 4, 7: 1, 0x66: 0})
        self.invite(self.alice, squad, [self.bob])
        self.bob.call(G, "AcceptInvitation", {"gameSessionInvitation": self.received(self.bob)[0]})
        self.add(self.alice, squad, private=[self.bob])
        values = self.attributes(self.carol, squad)
        self.assertEqual((values[5], values[6]), (0, 1))

    def test_somebody_who_is_away_finds_it_later(self):
        k = self.host(self.alice)
        self.on_server(lambda: self.server.backend.clients.drop(
            self.server.backend.clients.by_pid(self.bob.pid)))
        self.invite(self.alice, k, [self.bob])
        self.assertTrue(self.wait_logged("invited bob to session %d (not here" % k["m_sessionID"]))
        bob = self.player("bob")
        self.assertEqual([i["m_senderPID"] for i in self.received(bob)], [self.alice.pid])

    def test_nobody_and_oneself_are_skipped(self):
        k = self.host(self.alice)
        self.alice.call(G, "SendInvitation", {"invitation": {
            "m_sessionKey": k, "m_recipientPIDs": [0x7777, self.alice.pid, self.bob.pid],
            "m_message": "hi"}})
        self.assertEqual(len(self.received(self.bob)), 1)
        self.assertEqual(self.received(self.alice), [])

    def test_a_text_message_is_taken(self):
        k = self.host(self.alice)
        self.alice.call(G, "SendTextMessage", {"gameSessionMessage": {
            "m_sessionKey": k, "m_message": "ready?"}})
        self.assertTrue(self.logged("said to session %d: 'ready?'" % k["m_sessionID"]))


class FriendsScreen(Sessions):
    def in_session(self, who, pids, type_id=TYPE) -> list:
        return who.call(G, "SearchSessionsWithParticipants", {
            "gameSessionTypeID": type_id, "participantIDs": pids})["searchResults"]

    def test_which_friends_are_playing(self):
        k = self.host(self.alice)
        self.add(self.alice, k, [self.bob])
        rows = self.in_session(self.carol, [self.alice.pid, self.bob.pid, 0x7777])
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["m_sessionKey"], k)
        self.assertEqual(row["m_hostPID"], self.alice.pid)
        self.assertEqual(row["m_participantIDs"], [self.alice.pid, self.bob.pid])
        self.assertEqual(row["m_hostURLs"], self.client_of(self.alice).urls)
        values = as_dict(row["m_attributes"])
        self.assertEqual([values[a] for a in (3, 4, 5, 6)], [4, 0, 2, 0])
        self.assertEqual(len(self.in_session(self.carol, [self.bob.pid])), 1)

    def test_nobody_playing_is_an_empty_list(self):
        self.host(self.alice)
        self.assertEqual(self.in_session(self.carol, []), [])
        self.assertEqual(self.in_session(self.carol, [self.bob.pid]), [])
        self.assertEqual(self.in_session(self.carol, [self.alice.pid], type_id=2), [])

    def test_a_session_without_a_host_is_not_joinable(self):
        k = self.host(self.alice)
        self.add(self.alice, k, [self.bob])
        self.alice.call(G, "AbandonSession", {"gameSessionKey": k})
        self.assertEqual(self.in_session(self.carol, [self.bob.pid]), [])

    def test_several_at_once(self):
        first = self.host(self.alice)
        second = self.host(self.bob)
        gone = self.host(self.carol)
        self.carol.call(G, "AbandonSession", {"gameSessionKey": gone})
        rows = self.carol.call(G, "GetSessions", {"gameSessionKeys": [first, gone, second]})
        self.assertEqual([r["m_sessionKey"] for r in rows["searchResults"]], [first, second])


class Forgetting(Sessions):
    def forget(self, *players):
        def backdate():
            for player in players:
                self.server.backend.clients.by_pid(player.pid).last_seen -= FORGET_AFTER + 1
            return self.server.backend.forget_quiet_clients()
        return self.on_server(backdate)

    def test_a_forgotten_host(self):
        k = self.host(self.alice)
        self.add(self.alice, k, [self.bob])
        self.invite_to(self.bob, k)
        self.assertEqual(len(self.forget(self.alice)), 1)
        self.assertTrue(self.logged("alice is out of session %d" % k["m_sessionID"]))
        result = self.get(self.bob, k)
        self.assertEqual(result["m_hostPID"], 0)
        self.assertEqual(as_dict(result["m_attributes"])[5], 1)
        self.assertEqual(self.participants(self.bob, k), [self.bob.pid])
        self.assertEqual(self.found(self.carol), [])
        self.bob.call(G, "MigrateSession", {"gameSessionKey": k})
        self.assertEqual(self.found(self.carol), [k["m_sessionID"]])
        # And when the last of them goes too, so does the session.
        self.forget(self.bob)
        self.assertIsNone(self.on_server(lambda: self.server.backend.sessions.get(
            k["m_sessionID"])))

    def invite_to(self, host, k):
        host.call(G, "SendInvitation", {"invitation": {
            "m_sessionKey": k, "m_recipientPIDs": [self.carol.pid], "m_message": "hi"}})

    def test_a_forgotten_player_loses_its_invitations_and_urls(self):
        k = self.host(self.alice)
        self.carol.call(G, "RegisterURLs", {"stationURLs": ["prudp:/address=10.9.9.9;port=1"]})
        self.invite_to(self.alice, k)
        self.forget(self.carol)
        state = self.on_server(lambda: (list(self.server.backend.sessions.invitations),
                                        dict(self.server.backend.sessions.urls)))
        self.assertEqual(state[0], [])
        self.assertNotIn(self.carol.pid, state[1])
        self.assertEqual(self.get(self.bob, k)["m_hostPID"], self.alice.pid)

    def test_the_status_page_lists_them(self):
        k = self.host(self.alice)
        self.add(self.alice, k, [self.bob])
        status = self.on_server(self.server.backend.status)
        self.assertEqual(len(status["sessions"]), 1)
        row = status["sessions"][0]
        self.assertEqual((row["id"], row["host_name"], row["public"], row["used"]),
                         (k["m_sessionID"], "alice", [self.alice.pid, self.bob.pid], [2, 0]))


class Restarting(Sessions):
    """A game that crashed and was started again: same pid, a new connection, no sessions."""

    def restart(self, player):
        player.close()
        again = self.player(player.name)
        self.assertEqual(again.pid, player.pid)
        return again

    def friends_refresh(self, who, pids) -> list:
        return who.call(G, "SearchSessionsWithParticipants", {
            "gameSessionTypeID": TYPE, "participantIDs": pids})["searchResults"]

    def test_a_restarted_host_is_not_offered_and_its_session_waits(self):
        k = self.host(self.alice)
        self.add(self.alice, k, [self.bob])
        alice = self.restart(self.alice)
        # The new game has said nothing here yet, and the old one's session already waits for a
        # new host: it is not offered, and the friends list does not show it.
        self.assertEqual(self.found(self.carol), [])
        self.assertTrue(self.logged("alice was restarted: out of session %d" % k["m_sessionID"]))
        self.assertEqual(self.friends_refresh(self.carol, [self.bob.pid]), [])
        self.assertEqual(self.friends_refresh(alice, [self.bob.pid]), [])
        self.assertEqual(self.count_logged("alice was restarted"), 1)
        result = self.get(self.bob, k)
        self.assertEqual(result["m_hostPID"], 0)
        self.assertEqual(self.participants(self.bob, k), [self.bob.pid])
        self.assertEqual(as_dict(result["m_attributes"])[5], 1)
        # The member who stayed takes it over, and it is offered again - to alice too.
        self.bob.call(G, "MigrateSession", {"gameSessionKey": k})
        self.assertEqual(self.found(alice), [k["m_sessionID"]])
        self.assertEqual(self.found(self.carol), [k["m_sessionID"]])

    def test_a_restarted_host_alone_takes_its_session_with_it(self):
        k = self.host(self.alice)
        old_urls = ["prudp:/address=10.1.1.1;port=3074;sid=15;type=3"]
        self.alice.call(G, "RegisterURLs", {"stationURLs": old_urls})
        theirs = self.host(self.bob)
        self.bob.call(G, "SendInvitation", {"invitation": {
            "m_sessionKey": theirs, "m_recipientPIDs": [self.alice.pid], "m_message": "hi"}})
        alice = self.restart(self.alice)
        got = alice.call(G, "GetInvitationsReceived", {
            "gameSessionTypeID": TYPE, "resultRange": {"m_uiOffset": 0, "m_uiSize": 25}})
        # The invitation is still good: bob's session never went anywhere.
        self.assertEqual([i["m_sessionKey"] for i in got["invitations"]], [theirs])
        self.fails(Errors.SESSION_INVALID_SESSION_KEY, self.carol, "GetSession",
                   {"gameSessionKey": k})
        # What the old game registered is not where the new one is.
        mine = self.host(alice)
        self.assertEqual(self.get(self.carol, mine)["m_hostURLs"], self.client_of(alice).urls)
        self.assertNotEqual(self.get(self.carol, mine)["m_hostURLs"], old_urls)

    def test_a_restarted_member_is_out_before_its_host_says_so(self):
        k = self.host(self.alice)
        self.add(self.alice, k, [self.bob, self.carol])
        self.restart(self.bob)
        self.assertEqual(self.participants(self.alice, k), [self.alice.pid, self.carol.pid])
        self.assertEqual(self.attributes(self.carol, k)[5], 2)
        self.assertEqual(self.get(self.carol, k)["m_hostPID"], self.alice.pid)

    def test_the_same_game_calling_again_is_no_restart(self):
        k = self.host(self.alice)
        for _ in range(3):
            self.friends_refresh(self.alice, [self.bob.pid])
        self.assertEqual(self.get(self.bob, k)["m_hostPID"], self.alice.pid)
        self.assertFalse(self.logged("was restarted"))


class NotKept(unittest.TestCase):
    def test_sessions_do_not_survive_a_restart(self):
        data_dir = tempfile.mkdtemp(prefix="grfs-test-")
        self.addCleanup(shutil.rmtree, data_dir, True)
        with Harness(data_dir=data_dir) as server:
            alice = server.player("alice")
            k = alice.call(G, "CreateSession", {"gameSession": {
                "m_typeID": TYPE, "m_attributes": props(MATCH)}})["gameSessionKey"]
            server.assert_clean()
        with Harness(data_dir=data_dir) as server:
            alice = server.player("alice")
            with self.assertRaises(RmcFailure) as caught:
                alice.call(G, "GetSession", {"gameSessionKey": k})
            self.assertEqual(caught.exception.code, Errors.SESSION_INVALID_SESSION_KEY)
            self.assertEqual(alice.call(G, "SearchSessionsWithParticipants", {
                "gameSessionTypeID": TYPE, "participantIDs": [alice.pid]})["searchResults"], [])
            server.assert_clean()
