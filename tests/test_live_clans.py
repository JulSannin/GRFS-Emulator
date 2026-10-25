"""ClansProtocol against a running server: founding, invitations, ranks, the calendar, kept."""

import datetime
import json
import os
import shutil
import tempfile
import unittest

from grfs import clans as model
from grfs import fakegame
from grfs.harness import Harness
from grfs.rmc.router import Errors
from grfs.rmc.stream import pack_datetime

from tests.live import LiveTest

C = "ClansProtocol"
OK = model.OK


def call(player, method, **args):
    return player.call(C, method, args)


def result(player, method, **args):
    return call(player, method, **args)["result"]


def create(player, name, tag="", description=""):
    reply = call(player, "CreateClan", name=name, tag=tag, description=description)
    if reply["result"] != OK:
        raise AssertionError("CreateClan %r answered 0x%X" % (name, reply["result"]))
    return reply["clanId"]


def current(player):
    return call(player, "GetCurUserClan")["clanId"]


def info(player, clan_id):
    clans = call(player, "GetClanList", clanIds=[clan_id])["clans"]
    return clans[0] if clans else None


def members(player, clan_id=0):
    """name -> role"""
    return {m["name"]: m["role"] for m in call(player, "GetClanMembers", clanId=clan_id)["members"]}


def invites(player, clan_id=0):
    reply = call(player, "GetClanInvitesWithNames", clanId=clan_id)
    return reply["result"], [(i["pid"], i["clanId"], i["inviterPid"], i["name"])
                             for i in reply["invites"]]


def recruit(leader, *players):
    """Invite each player into the leader's clan and have them accept."""
    clan_id = current(leader)
    for p in players:
        if result(leader, "InviteToClan", pid=p.pid, inviteType=7) != OK:
            raise AssertionError("could not invite %s" % p.name)
        if result(p, "JoinClan", clanId=clan_id) != OK:
            raise AssertionError("%s could not join" % p.name)
    return clan_id


def when(*args):
    return pack_datetime(datetime.datetime(*args))


def event(text, event_type=1, start=when(2026, 11, 1, 20), end=when(2026, 11, 1, 22),
          event_id=0, clan_id=0, author=0):
    return {"id": event_id, "clanId": clan_id, "authorPid": author, "text": text,
            "eventType": event_type, "start": start, "end": end}


def events(player, clan_id=0, event_type=0, since=0, until=0):
    return [(e["text"], e["eventType"]) for e in call(
        player, "GetClanEvents", clanId=clan_id, eventType=event_type, since=since,
        until=until)["events"]]


class Founding(LiveTest):
    def setUp(self):
        super().setUp()
        self.alice = self.player("alice")
        self.bob = self.player("bob")

    def test_a_new_player_is_in_no_clan(self):
        self.assertEqual(current(self.alice), 0)
        self.assertEqual(members(self.alice), {})
        self.assertEqual(invites(self.alice), (OK, []))

    def test_founding_makes_the_founder_its_leader(self):
        clan_id = create(self.alice, "Ghosts", "GST", "Hunting Bodark")
        self.assertNotEqual(clan_id, 0)
        self.assertEqual(current(self.alice), clan_id)
        self.assertEqual(info(self.bob, clan_id), {
            "id": clan_id, "name": "Ghosts", "tag": "GST", "description": "Hunting Bodark",
            "flags": 0, "rolePrivileges": [0xFFFF] * 4, "properties": [0] * 21, "extra": ""})
        roster = call(self.bob, "GetClanMembers", clanId=clan_id)["members"]
        self.assertEqual([(m["pid"], m["role"], m["name"], m["online"]) for m in roster],
                         [(self.alice.pid, model.LEADER, "alice", 1)])
        self.assertIsNotNone(roster[0]["joined"])
        self.assertEqual(current(self.bob), 0)

    def test_a_clan_that_is_not_there_is_left_out(self):
        clan_id = create(self.alice, "Ghosts", "GST")
        clans = call(self.bob, "GetClanList", clanIds=[999, clan_id])["clans"]
        self.assertEqual([c["id"] for c in clans], [clan_id])
        self.assertEqual(members(self.bob, 999), {})

    def test_one_clan_per_player(self):
        """The client writes CreateClan's clan id into its current clan whatever the result, so
        a refusal must answer the clan the player is in, not 0."""
        clan_id = create(self.alice, "Ghosts", "GST")
        reply = call(self.alice, "CreateClan", name="Others", tag="OTH", description="")
        self.assertEqual(reply, {"clanId": clan_id, "result": model.ALREADY_IN_A_CLAN})
        self.assertEqual(current(self.alice), clan_id)
        recruit(self.alice, self.bob)
        reply = call(self.bob, "CreateClan", name="Ghosts", tag="GST", description="")
        self.assertEqual(reply, {"clanId": clan_id, "result": model.ALREADY_IN_A_CLAN})
        self.assertEqual(call(self.bob, "GetClanList", clanIds=[clan_id + 1])["clans"], [])

    def test_names_are_unique_whatever_the_case(self):
        create(self.alice, "Ghosts", "GST")
        reply = call(self.bob, "CreateClan", name="GHOSTS", tag="G2", description="")
        self.assertEqual(reply, {"clanId": 0, "result": model.NAME_TAKEN})
        self.assertNotEqual(create(self.bob, "Ghosts Two", "GST"), 0)   # a tag may repeat

    def test_lengths_the_client_checks_are_checked_here_too(self):
        for name, tag, description in (("", "A", ""), ("x" * 31, "A", ""),
                                       ("Fine", "ABCD", ""), ("Fine", "A", "d" * 81)):
            reply = call(self.alice, "CreateClan", name=name, tag=tag, description=description)
            self.assertEqual(reply, {"clanId": 0, "result": model.INVALID}, (name, tag))
        self.assertNotEqual(create(self.alice, "x" * 30, "ABC", "d" * 80), 0)

    def test_the_leader_changes_tag_and_description_each_on_its_own(self):
        clan_id = create(self.alice, "Ghosts", "GST", "first")
        self.assertEqual(result(self.alice, "UpdateClan", tag="NEW", description="ignored",
                                fields=1), OK)
        self.assertEqual((info(self.alice, clan_id)["tag"],
                          info(self.alice, clan_id)["description"]), ("NEW", "first"))
        self.assertEqual(result(self.alice, "UpdateClan", tag="ignored", description="second",
                                fields=2), OK)
        self.assertEqual((info(self.alice, clan_id)["tag"],
                          info(self.alice, clan_id)["description"]), ("NEW", "second"))
        self.assertEqual(result(self.alice, "UpdateClan", tag="TOOLONG", description="",
                                fields=1), model.INVALID)

    def test_only_the_leader_changes_the_clan(self):
        clan_id = create(self.alice, "Ghosts", "GST", "mine")
        recruit(self.alice, self.bob)
        self.assertEqual(result(self.bob, "UpdateClan", tag="BOB", description="", fields=3),
                         model.NOT_ALLOWED)
        self.assertEqual(result(self.bob, "ChangeClanRolePriv", role=3, privileges=0),
                         model.NOT_ALLOWED)
        self.assertEqual(info(self.alice, clan_id)["tag"], "GST")

    def test_somebody_in_no_clan_is_told_so(self):
        """0x324 is the one code the client acts on: it clears its clan id."""
        self.assertEqual(result(self.alice, "UpdateClan", tag="X", description="", fields=1),
                         model.NOT_IN_A_CLAN)
        self.assertEqual(result(self.alice, "InviteToClan", pid=self.bob.pid, inviteType=7),
                         model.NOT_IN_A_CLAN)
        self.assertEqual(result(self.alice, "LeaveClan", pid=self.alice.pid),
                         model.NOT_IN_A_CLAN)
        self.assertEqual(result(self.alice, "SetClanEvent", event=event("x")),
                         model.NOT_IN_A_CLAN)

    def test_the_leader_sets_what_each_role_may_do(self):
        clan_id = create(self.alice, "Ghosts", "GST")
        self.assertEqual(result(self.alice, "ChangeClanRolePriv", role=2, privileges=0x0013), OK)
        self.assertEqual(result(self.alice, "ChangeClanRolePriv", role=3, privileges=0x10001),
                         OK)
        self.assertEqual(info(self.alice, clan_id)["rolePrivileges"],
                         [0xFFFF, 0xFFFF, 0x0013, 0x0001])
        self.assertEqual(result(self.alice, "ChangeClanRolePriv", role=0, privileges=0),
                         model.INVALID)
        self.assertEqual(result(self.alice, "ChangeClanRolePriv", role=4, privileges=0),
                         model.INVALID)


class Membership(LiveTest):
    def setUp(self):
        super().setUp()
        self.alice = self.player("alice")
        self.bob = self.player("bob")
        self.carol = self.player("carol")
        self.clan = create(self.alice, "Ghosts", "GST")

    def test_an_invitation_shows_on_both_sides_and_accepting_joins(self):
        a, b = self.alice, self.bob
        self.assertEqual(result(a, "InviteToClan", pid=b.pid, inviteType=7), OK)
        self.assertEqual(invites(b), (OK, [(b.pid, self.clan, a.pid, "alice")]))
        self.assertEqual(invites(a, self.clan), (OK, [(b.pid, self.clan, a.pid, "bob")]))
        self.assertEqual(current(b), 0)
        self.assertEqual(result(b, "JoinClan", clanId=self.clan), OK)
        self.assertEqual(current(b), self.clan)
        self.assertEqual(members(a), {"alice": model.LEADER, "bob": model.NEWCOMER})
        self.assertEqual(invites(b), (OK, []))
        self.assertEqual(invites(a, self.clan), (OK, []))

    def test_inviting_by_name(self):
        self.assertEqual(result(self.alice, "InviteToClanWithName", name="BOB", inviteType=7),
                         OK)
        self.assertEqual(invites(self.bob)[1], [(self.bob.pid, self.clan, self.alice.pid,
                                                 "alice")])

    def test_inviting_a_stranger_is_refused(self):
        self.assertEqual(result(self.alice, "InviteToClanWithName", name="nobody",
                                inviteType=7), model.NO_SUCH_PLAYER)
        self.assertEqual(result(self.alice, "InviteToClan", pid=0x7777777, inviteType=7),
                         model.NO_SUCH_PLAYER)
        self.assertEqual(invites(self.alice, self.clan), (OK, []))

    def test_inviting_somebody_in_a_clan_is_refused(self):
        create(self.bob, "Others", "OTH")
        self.assertEqual(result(self.alice, "InviteToClan", pid=self.bob.pid, inviteType=7),
                         model.ALREADY_IN_A_CLAN)
        recruit(self.alice, self.carol)
        self.assertEqual(result(self.alice, "InviteToClan", pid=self.carol.pid, inviteType=7),
                         model.INVALID)
        self.assertEqual(result(self.alice, "InviteToClan", pid=self.alice.pid, inviteType=7),
                         model.INVALID)

    def test_joining_takes_an_invitation(self):
        self.assertEqual(result(self.bob, "JoinClan", clanId=self.clan), model.NOT_INVITED)
        self.assertEqual(result(self.bob, "JoinClan", clanId=999), model.NO_SUCH_CLAN)
        self.assertEqual(current(self.bob), 0)

    def test_joining_drops_every_other_invitation(self):
        other = create(self.carol, "Others", "OTH")
        result(self.alice, "InviteToClan", pid=self.bob.pid, inviteType=7)
        result(self.carol, "InviteToClan", pid=self.bob.pid, inviteType=7)
        self.assertEqual(len(invites(self.bob)[1]), 2)
        self.assertEqual(result(self.bob, "JoinClan", clanId=other), OK)
        self.assertEqual(invites(self.bob), (OK, []))
        self.assertEqual(result(self.bob, "JoinClan", clanId=self.clan),
                         model.ALREADY_IN_A_CLAN)

    def test_declining_and_taking_back(self):
        a, b, c = self.alice, self.bob, self.carol
        result(a, "InviteToClan", pid=b.pid, inviteType=7)
        result(a, "InviteToClan", pid=c.pid, inviteType=7)
        self.assertEqual(result(b, "RemoveClanInvite", pid=b.pid, clanId=self.clan), OK)
        self.assertEqual(result(a, "RemoveClanInvite", pid=c.pid, clanId=0), OK)
        self.assertEqual(invites(a, self.clan), (OK, []))
        self.assertEqual(result(b, "JoinClan", clanId=self.clan), model.NOT_INVITED)
        self.assertEqual(result(b, "RemoveClanInvite", pid=b.pid, clanId=self.clan),
                         model.NOT_INVITED)
        self.assertEqual(result(a, "RemoveClanInvite", pid=c.pid, clanId=0), model.NOT_INVITED)

    def test_declining_takes_the_callers_own_invitation_whatever_pid_comes_with_it(self):
        """The client sends the pid its script gave it unchanged and finds the invitation by its
        own pid - so the pid says nothing about whose invitation it is."""
        a, b, c = self.alice, self.bob, self.carol
        result(a, "InviteToClan", pid=b.pid, inviteType=7)
        result(a, "InviteToClan", pid=c.pid, inviteType=7)
        self.assertEqual(result(b, "RemoveClanInvite", pid=a.pid, clanId=self.clan), OK)
        self.assertEqual(result(c, "RemoveClanInvite", pid=0, clanId=self.clan), OK)
        self.assertEqual(invites(a, self.clan), (OK, []))
        result(a, "InviteToClan", pid=c.pid, inviteType=7)
        # Somebody else's pid does not decline for them: it is still the caller's own.
        self.assertEqual(result(b, "RemoveClanInvite", pid=c.pid, clanId=self.clan),
                         model.NOT_INVITED)
        self.assertEqual(invites(c), (OK, [(c.pid, self.clan, a.pid, "alice")]))

    def test_a_clan_shows_its_invitations_to_its_own_members_only(self):
        result(self.alice, "InviteToClan", pid=self.bob.pid, inviteType=7)
        self.assertEqual(invites(self.carol, self.clan), (model.NOT_ALLOWED, []))

    def test_ranks(self):
        a, b, c = self.alice, self.bob, self.carol
        recruit(a, b, c)
        self.assertEqual(result(a, "ChangeClanRole", pid=b.pid, role=1), OK)
        self.assertEqual(result(b, "ChangeClanRole", pid=c.pid, role=2), OK)
        self.assertEqual(members(a), {"alice": 0, "bob": 1, "carol": 2})
        # Nobody acts on their equal or better, nor hands out their own rank or better.
        self.assertEqual(result(c, "ChangeClanRole", pid=b.pid, role=3), model.NOT_ALLOWED)
        self.assertEqual(result(b, "ChangeClanRole", pid=c.pid, role=1), model.NOT_ALLOWED)
        self.assertEqual(result(b, "ChangeClanRole", pid=a.pid, role=2), model.NOT_ALLOWED)
        self.assertEqual(result(b, "ChangeClanRole", pid=c.pid, role=0), model.NOT_ALLOWED)
        self.assertEqual(result(a, "ChangeClanRole", pid=a.pid, role=1), model.INVALID)
        self.assertEqual(result(a, "ChangeClanRole", pid=c.pid, role=4), model.INVALID)
        self.assertEqual(members(a), {"alice": 0, "bob": 1, "carol": 2})

    def test_making_somebody_leader_hands_the_clan_over(self):
        recruit(self.alice, self.bob)
        self.assertEqual(result(self.alice, "ChangeClanRole", pid=self.bob.pid, role=0), OK)
        self.assertEqual(members(self.alice), {"alice": 1, "bob": 0})
        self.assertEqual(result(self.alice, "UpdateClan", tag="X", description="", fields=1),
                         model.NOT_ALLOWED)
        self.assertEqual(result(self.bob, "UpdateClan", tag="BOB", description="", fields=1),
                         OK)

    def test_leaving_and_being_kicked(self):
        a, b, c = self.alice, self.bob, self.carol
        recruit(a, b, c)
        result(a, "ChangeClanRole", pid=b.pid, role=1)
        self.assertEqual(result(c, "LeaveClan", pid=b.pid), model.NOT_ALLOWED)
        self.assertEqual(result(b, "LeaveClan", pid=a.pid), model.NOT_ALLOWED)
        self.assertEqual(result(b, "LeaveClan", pid=c.pid), OK)
        self.assertEqual(current(c), 0)
        self.assertEqual(result(b, "LeaveClan", pid=c.pid), model.NO_SUCH_PLAYER)
        self.assertEqual(result(b, "LeaveClan", pid=b.pid), OK)
        self.assertEqual(current(b), 0)
        self.assertEqual(members(a), {"alice": 0})

    def test_a_leader_who_leaves_leaves_the_clan_to_the_best_ranked(self):
        a, b, c = self.alice, self.bob, self.carol
        recruit(a, b, c)
        result(a, "ChangeClanRole", pid=c.pid, role=1)
        self.assertEqual(result(a, "LeaveClan", pid=a.pid), OK)
        self.assertEqual(members(b, self.clan), {"bob": 3, "carol": 0})
        self.assertEqual(current(a), 0)

    def test_the_last_one_out_disbands_the_clan(self):
        result(self.alice, "InviteToClan", pid=self.bob.pid, inviteType=7)
        self.assertEqual(result(self.alice, "LeaveClan", pid=self.alice.pid), OK)
        self.assertIsNone(info(self.bob, self.clan))
        self.assertEqual(invites(self.bob), (OK, []))
        self.assertEqual(result(self.bob, "JoinClan", clanId=self.clan), model.NO_SUCH_CLAN)
        # The name is free again, and the id is never given out twice.
        self.assertNotIn(create(self.bob, "Ghosts", "GST"), (0, self.clan))

    def test_clan_ids_and_tags_for_a_mix_of_players(self):
        a, b, c = self.alice, self.bob, self.carol
        dave = self.player("dave")
        other = create(b, "Others", "OTH")
        recruit(a, dave)
        reply = call(c, "GetClanIdAndTags", pids=[a.pid, b.pid, c.pid, 0x7777777, dave.pid])
        self.assertEqual(reply, {"clanIds": [self.clan, other, 0, 0, self.clan]})
        tags = call(c, "GetClanTags", clanIds=[self.clan, other, 0, 999, self.clan])
        self.assertEqual(tags, {"tags": ["GST", "OTH", "", "", "GST"]})
        self.assertEqual(call(c, "GetClanIdAndTags", pids=[]), {"clanIds": []})


class Calendar(LiveTest):
    def setUp(self):
        super().setUp()
        self.alice = self.player("alice")
        self.bob = self.player("bob")
        self.carol = self.player("carol")
        self.clan = create(self.alice, "Ghosts", "GST")
        recruit(self.alice, self.bob)

    def ids(self, player):
        return [e["id"] for e in call(player, "GetClanEvents", clanId=self.clan, eventType=0,
                                      since=0, until=0)["events"]]

    def test_events_are_added_listed_and_filtered(self):
        a = self.alice
        self.assertEqual(result(a, "SetClanEvent", event=event("raid", 1, when(2026, 11, 2, 20),
                                                             when(2026, 11, 2, 22))), OK)
        self.assertEqual(result(a, "SetClanEvent", event=event("training", 2,
                                                             when(2026, 11, 1, 18),
                                                             when(2026, 11, 1, 19))), OK)
        self.assertEqual(events(self.carol, self.clan), [("training", 2), ("raid", 1)])
        self.assertEqual(events(a, 0, event_type=1), [("raid", 1)])
        self.assertEqual(events(a, since=when(2026, 11, 2), until=when(2026, 11, 3)),
                         [("raid", 1)])
        self.assertEqual(events(a, until=when(2026, 11, 1, 23)), [("training", 2)])
        stored = call(a, "GetClanEvents", clanId=self.clan, eventType=1, since=0,
                      until=0)["events"][0]
        self.assertEqual((stored["clanId"], stored["authorPid"], stored["start"]),
                         (self.clan, a.pid, when(2026, 11, 2, 20)))

    def test_changing_retyping_and_removing(self):
        a = self.alice
        result(a, "SetClanEvent", event=event("raid"))
        event_id, = self.ids(a)
        self.assertEqual(result(a, "SetClanEvent", event=event("raid at nine", 3,
                                                             event_id=event_id)), OK)
        self.assertEqual(events(a), [("raid at nine", 3)])
        self.assertEqual(result(a, "ChangeEventType", eventId=event_id, eventType=4), OK)
        self.assertEqual(events(a), [("raid at nine", 4)])
        self.assertEqual(result(a, "ChangeEventType", eventId=event_id, eventType=0),
                         model.INVALID)
        self.assertEqual(result(a, "RemoveClanEvent", eventId=event_id), OK)
        self.assertEqual(events(a), [])
        self.assertEqual(result(a, "RemoveClanEvent", eventId=event_id), model.NO_SUCH_EVENT)

    def test_a_member_changes_only_their_own_events(self):
        a, b = self.alice, self.bob
        result(a, "SetClanEvent", event=event("the leader's"))
        result(b, "SetClanEvent", event=event("bob's"))
        leaders, bobs = self.ids(a)
        self.assertEqual(result(b, "RemoveClanEvent", eventId=leaders), model.NOT_ALLOWED)
        self.assertEqual(result(b, "ChangeEventType", eventId=leaders, eventType=2),
                         model.NOT_ALLOWED)
        self.assertEqual(result(b, "SetClanEvent", event=event("mine now", event_id=leaders)),
                         model.NOT_ALLOWED)
        self.assertEqual(result(b, "ChangeEventType", eventId=bobs, eventType=2), OK)
        self.assertEqual(result(a, "RemoveClanEvent", eventId=bobs), OK)    # the leader may
        self.assertEqual(events(b), [("the leader's", 1)])

    def test_another_clans_events_are_out_of_reach(self):
        result(self.alice, "SetClanEvent", event=event("ours"))
        event_id, = self.ids(self.alice)
        create(self.carol, "Others", "OTH")
        self.assertEqual(result(self.carol, "RemoveClanEvent", eventId=event_id),
                         model.NO_SUCH_EVENT)
        self.assertEqual(result(self.carol, "SetClanEvent",
                                event=event("theirs", clan_id=self.clan)), model.NOT_ALLOWED)
        self.assertEqual(events(self.carol), [])

    def test_an_event_needs_a_type_and_text_that_fits_the_clients_buffer(self):
        """The client converts the text into 256 bytes of UTF-8, so 255 bytes is the longest."""
        self.assertEqual(result(self.alice, "SetClanEvent", event=event("x", event_type=0)),
                         model.INVALID)
        self.assertEqual(result(self.alice, "SetClanEvent", event=event("x" * 256)),
                         model.INVALID)
        self.assertEqual(result(self.alice, "SetClanEvent", event=event("\u00e9" * 128)),
                         model.INVALID)
        self.assertEqual(result(self.alice, "SetClanEvent", event=event("x" * 255)), OK)
        self.assertEqual(result(self.alice, "SetClanEvent", event=event("\u00e9" * 127)), OK)
        self.assertEqual(len(events(self.bob)), 2)


class Properties(LiveTest):
    def setUp(self):
        super().setUp()
        self.alice = self.player("alice")
        self.bob = self.player("bob")
        self.carol = self.player("carol")
        self.clan = create(self.alice, "Ghosts", "GST")
        recruit(self.alice, self.bob)

    def set(self, player, *changes, clan_id=None):
        return call(player, "SetClanProperty", clanId=self.clan if clan_id is None else clan_id,
                    properties=[{"propertyId": p, "value": v, "op": op} for p, v, op in changes])

    def test_properties_apply_their_operation(self):
        self.assertEqual(self.set(self.alice, (1, 10, model.ADD), (2, 7, model.SET),
                                  (3, 5, model.MAX)), {})
        self.set(self.bob, (1, 5, model.ADD), (2, 3, model.SUB), (3, 4, model.MAX),
                 (20, 9, model.MIN), (30, 1, model.SET))
        properties = info(self.carol, self.clan)["properties"]
        self.assertEqual(len(properties), 21)
        self.assertEqual(properties[:4] + properties[20:], [0, 15, 4, 5, 9])

    def test_only_a_member_sets_them(self):
        create(self.carol, "Others", "OTH")
        with self.assertRaises(fakegame.RmcFailure) as caught:
            self.set(self.carol, (1, 10, model.SET))
        self.assertEqual(caught.exception.code, Errors.CORE_ACCESS_DENIED)
        with self.assertRaises(fakegame.RmcFailure):
            self.set(self.player("dave"), (1, 10, model.SET), clan_id=0)
        self.assertEqual(info(self.alice, self.clan)["properties"][1], 0)

    def test_the_leaderboard(self):
        other = create(self.carol, "Others", "OTH")
        dave = self.player("dave")
        third = create(dave, "Third", "THR")
        self.set(self.alice, (5, 100, model.SET), (6, 1, model.SET))
        self.set(self.carol, (5, 300, model.SET), clan_id=other)
        board = call(self.bob, "GetClanLeaderboard", propertyId=5, offset=0, size=10)
        self.assertEqual([(e["clanId"], e["name"]) for e in board["entries"]],
                         [(other, "Others"), (self.clan, "Ghosts"), (third, "Third")])
        self.assertEqual(board["entries"][1]["stats"],
                         [{"value": 100, "propertyId": 5}, {"value": 1, "propertyId": 6}])
        self.assertEqual((board["ownRank"], board["total"]), (2, 3))
        page = call(self.bob, "GetClanLeaderboard", propertyId=5, offset=1, size=1)
        self.assertEqual([e["clanId"] for e in page["entries"]], [self.clan])
        clanless = call(self.player("erin"), "GetClanLeaderboard", propertyId=5, offset=0,
                        size=10)
        self.assertEqual((clanless["ownRank"], clanless["total"]), (0xFFFFFFFF, 3))


class Rooms(LiveTest):
    def setUp(self):
        super().setUp()
        self.alice = self.player("alice")
        self.bob = self.player("bob")
        self.clan = create(self.alice, "Ghosts", "GST")
        recruit(self.alice, self.bob)

    def rooms(self, player):
        return [(r["room"], r["state"], r["data"]) for r in
                call(player, "DownloadClanRoomInfo", first=0, count=10)["rooms"]]

    def host(self, player, data, room=3, state=0):
        return result(player, "SetClanRoom", clanId=self.clan, hostData=data, room=room,
                      state=state, param2=0)

    def test_a_host_walks_into_an_empty_room_then_publishes_it(self):
        """The client sends JoinClanRoom before SetClanRoom, so an empty room is no refusal."""
        a, b = self.alice, self.bob
        self.assertEqual(self.rooms(b), [])
        self.assertEqual(call(a, "JoinClanRoom", room=3), {})
        self.assertEqual(self.host(a, "host:alice"), OK)
        self.assertEqual(self.rooms(b), [(3, 0, "host:alice")])
        self.assertEqual(call(b, "JoinClanRoom", room=3), {})
        self.assertEqual(call(a, "JoinClanRoom", room=3), {})     # walking in again keeps it
        self.assertEqual(self.host(a, "host:alice, again", state=1), OK)
        self.assertEqual(self.rooms(b), [(3, 1, "host:alice, again")])

    def test_the_room_state_says_full_or_not_and_never_closes_the_room(self):
        """SetClanRoomState is sent with 0 when a full session gets a free slot."""
        a, b = self.alice, self.bob
        self.host(a, "host:alice")
        self.assertEqual(call(a, "SetClanRoomState", clanId=self.clan, room=3, state=1), {})
        self.assertEqual(self.rooms(b), [(3, 1, "host:alice")])
        self.assertEqual(call(a, "SetClanRoomState", clanId=self.clan, room=3, state=0), {})
        self.assertEqual(self.rooms(b), [(3, 0, "host:alice")])
        with self.assertRaises(fakegame.RmcFailure) as caught:
            call(b, "SetClanRoomState", clanId=self.clan, room=3, state=1)
        self.assertEqual(caught.exception.code, Errors.CORE_ACCESS_DENIED)
        with self.assertRaises(fakegame.RmcFailure) as caught:
            call(a, "SetClanRoomState", clanId=self.clan, room=4, state=1)
        self.assertEqual(caught.exception.code, Errors.CORE_INVALID_ARGUMENT)
        self.assertEqual(self.rooms(b), [(3, 0, "host:alice")])

    def test_only_its_host_closes_a_room(self):
        a, b = self.alice, self.bob
        self.host(a, "host:alice")
        self.assertEqual(self.host(b, "host:bob"), model.NOT_ALLOWED)
        self.assertEqual(self.host(b, "", state=2), OK)            # nothing of bob's to close
        self.assertEqual(self.rooms(b), [(3, 0, "host:alice")])
        # The host leaving: the client publishes the room again with no session.
        self.assertEqual(self.host(a, "", state=4), OK)
        self.assertEqual(self.rooms(b), [])
        self.assertEqual(self.host(b, "host:bob"), OK)             # free for somebody else
        self.assertEqual(self.rooms(a), [(3, 0, "host:bob")])

    def test_a_host_who_walks_out_closes_the_room(self):
        a, b = self.alice, self.bob
        self.host(a, "host:alice")
        self.host(b, "host:bob", room=5)
        self.assertEqual(call(a, "JoinClanRoom", room=5), {})          # into bob's room
        self.assertEqual(self.rooms(b), [(5, 0, "host:bob")])
        self.host(a, "host:alice", room=1)
        self.assertEqual(call(a, "JoinClanRoom", room=0), {})          # out of every room
        self.assertEqual(self.rooms(b), [(5, 0, "host:bob")])
        with self.assertRaises(fakegame.RmcFailure) as caught:
            call(a, "JoinClanRoom", room=11)
        self.assertEqual(caught.exception.code, Errors.CORE_INVALID_ARGUMENT)

    def test_rooms_are_the_clans_own(self):
        carol = self.player("carol")
        self.assertEqual(result(carol, "SetClanRoom", clanId=self.clan, hostData="x", room=1,
                                state=0, param2=0), model.NOT_IN_A_CLAN)
        self.assertEqual(call(carol, "DownloadClanRoomInfo", first=0, count=10), {"rooms": []})
        with self.assertRaises(fakegame.RmcFailure):
            call(carol, "JoinClanRoom", room=1)
        self.assertEqual(self.host(self.alice, "x", room=11), model.INVALID)
        create(carol, "Others", "OTH")
        self.assertEqual(self.host(self.alice, "x"), OK)
        self.assertEqual(call(carol, "DownloadClanRoomInfo", first=0, count=10), {"rooms": []})
        self.assertEqual(result(carol, "SetClanRoom", clanId=self.clan, hostData="y", room=3,
                                state=0, param2=0), model.NOT_ALLOWED)


class Keeping(unittest.TestCase):
    def setUp(self):
        self.data_dir = tempfile.mkdtemp(prefix="grfs-test-")
        self.addCleanup(shutil.rmtree, self.data_dir, True)

    def test_clans_survive_a_restart(self):
        with Harness(data_dir=self.data_dir) as server:
            alice, bob, carol = (server.player(n) for n in ("alice", "bob", "carol"))
            clan = create(alice, "Ghosts", "GST", "kept")
            recruit(alice, bob)
            result(alice, "ChangeClanRole", pid=bob.pid, role=2)
            result(alice, "ChangeClanRolePriv", role=2, privileges=0x00F0)
            gone = create(carol, "Gone", "GON")
            result(carol, "LeaveClan", pid=carol.pid)
            result(alice, "InviteToClan", pid=carol.pid, inviteType=7)
            call(bob, "SetClanProperty", clanId=clan,
                 properties=[{"propertyId": 4, "value": 1234, "op": model.ADD}])
            result(bob, "SetClanEvent", event=event("raid", 2))
            result(alice, "SetClanRoom", clanId=clan, hostData="x", room=1, state=0, param2=0)
            pids = alice.pid, bob.pid, carol.pid
            server.assert_clean()
        with open(os.path.join(self.data_dir, "clans.json"), encoding="utf-8") as f:
            self.assertEqual([c["name"] for c in json.load(f)["clans"]], ["Ghosts"])

        with Harness(data_dir=self.data_dir) as server:
            bob = server.player("bob")
            self.assertEqual(bob.pid, pids[1])
            self.assertEqual(current(bob), clan)          # at sign-in, from the file
            self.assertEqual(members(bob), {"alice": 0, "bob": 2})
            kept = info(bob, clan)
            self.assertEqual((kept["tag"], kept["description"], kept["rolePrivileges"],
                              kept["properties"][4]), ("GST", "kept", [0xFFFF, 0xFFFF, 0xF0,
                                                                       0xFFFF], 1234))
            self.assertEqual(events(bob), [("raid", 2)])
            carol = server.player("carol")
            self.assertEqual(current(carol), 0)
            self.assertEqual(invites(carol), (OK, [(pids[2], clan, pids[0], "alice")]))
            # Rooms point at games that are gone; ids are never reused.
            self.assertEqual(call(bob, "DownloadClanRoomInfo", first=0, count=10), {"rooms": []})
            self.assertNotIn(create(carol, "Newer", "NEW"), (clan, gone))
            self.assertEqual(call(bob, "GetClanIdAndTags", pids=list(pids)),
                             {"clanIds": [clan, clan, current(carol)]})
            server.assert_clean()

    def test_a_damaged_file_is_set_aside_not_overwritten(self):
        path = os.path.join(self.data_dir, "clans.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write('{"clans": [{"id": 1, "name"')
        with Harness(data_dir=self.data_dir) as server:
            self.assertTrue(server.capture.has("cannot read"))
            alice = server.player("alice")
            self.assertEqual(current(alice), 0)
            create(alice, "Ghosts", "GST")
        aside = [n for n in os.listdir(self.data_dir) if n.startswith("clans.json.bad-")]
        self.assertEqual(len(aside), 1)


if __name__ == "__main__":
    unittest.main()
