"""ClansProtocol (0x6E): the clan screen, and the clan tag beside a player's name.

The game calls 13 GetCurUserClan at every sign-in, and 26 then 27 to put clan tags beside other
players' names. Unanswered, the first is an error posted to the clan screen at every sign-in and
the player is in no clan; the others leave every tag blank.

Not in the game's DDL - this is a Hermes protocol - so the layouts below were read out of the
client: each TaskRDV_* constructor's fields (8C40C0 and its neighbours), the stub each task's
send step calls (8E7150 and the rest), and the reply parsers behind them (8EA150...). Methods 4,
10 and 25 are left out: the client has stubs for them and nothing that ever calls one.

How the client takes an answer, which is the same for every call that carries a u32 result:

* The task sets the result to 2 before it sends, and posts it to the clan screen as an error
  unless it comes back 0. An RMC failure is posted the same way, so nothing hangs either way;
  but a result code is the protocol's own way to say no, and it is what is answered here.
* 0x324 means "not in a clan": the client clears its clan id and redraws the screen for no clan
  (completion A6BBC0). It is answered to exactly the players who are in none.
* The client checks lengths, its own role, and roles past 3 before it sends anything, with codes
  of its own from 0x2710 - so those reach the server only from something that is not the game.

What the rules are - one clan per player, who outranks whom, who may do what - is in
grfs/clans.py.

CreateClan's clan id is not only for a clan that was made: the task writes it straight into the
client's current clan id whatever the result (8C3050 copies it before it looks), and the client
does not check before it sends that its player is in no clan. So a refusal answers the clan the
player is already in - 0 only for somebody in none - or the game would forget its own clan.

Several parameters are constants whose meaning the client does not reveal - InviteToClan's
second u32 is always 7, SetClanRoom's last byte is always 0 - and are logged and otherwise left
alone. A few values in replies are never read by the client: ClanInfo's byte and closing string
are 0 and "".
"""

from .. import clans as model
from .. import protocols
from ..rmc.router import Errors, RmcError, Service


class Clans(Service):
    protocol_id = protocols.CLANS
    protocol_name = "ClansProtocol"
    schema_classes = [
        # GetClanList's element: the role privileges indexed by role, the clan's properties
        # indexed by property id; the client keeps 4 and 21 of them.
        ("ClanInfo", [("id", "uint32"), ("name", "string"), ("tag", "string"),
                      ("description", "string"), ("flags", "byte"),
                      ("rolePrivileges", "qlist<uint16>"), ("properties", "qlist<uint32>"),
                      ("extra", "string")]),
        ("ClanMember", [("pid", "uint32"), ("role", "byte"), ("joined", "datetime"),
                        ("lastSeen", "datetime"), ("name", "string"), ("online", "byte")]),
        # One invitation, with the name of whoever is on the other side of it from the asker.
        ("ClanInvite", [("pid", "uint32"), ("clanId", "uint32"), ("inviterPid", "uint32"),
                        ("sent", "datetime"), ("name", "string")]),
        ("ClanEvent", [("id", "uint32"), ("clanId", "uint32"), ("authorPid", "uint32"),
                       ("text", "string"), ("eventType", "byte"), ("start", "datetime"),
                       ("end", "datetime")]),
        ("ClanPropertyChange", [("value", "uint32"), ("propertyId", "byte"), ("op", "byte")]),
        ("ClanRoom", [("room", "byte"), ("state", "byte"), ("data", "string")]),
        ("ClanStat", [("value", "uint32"), ("propertyId", "byte")]),
        ("ClanLeaderboardEntry", [("clanId", "uint32"), ("name", "string"),
                                  ("stats", "qlist<ClanStat>")]),
    ]
    schema_methods = {
        1: ("CreateClan", [("in", "string", "name"), ("in", "string", "tag"),
                           ("in", "string", "description"),
                           ("out", "uint32", "clanId"), ("out", "uint32", "result")], None),
        # The byte says which of the two strings to apply: 1 the tag, 2 the description.
        2: ("UpdateClan", [("in", "string", "tag"), ("in", "string", "description"),
                           ("in", "byte", "fields"), ("out", "uint32", "result")], None),
        3: ("GetClanList", [("in", "qlist<uint32>", "clanIds"),
                            ("out", "qlist<ClanInfo>", "clans")], None),
        5: ("InviteToClan", [("in", "uint32", "pid"), ("in", "uint32", "inviteType"),
                             ("out", "uint32", "result")], None),
        6: ("InviteToClanWithName", [("in", "string", "name"), ("in", "uint32", "inviteType"),
                                     ("out", "uint32", "result")], None),
        # Clan 0 is the caller's own clan taking back an invitation to pid; any other clan is the
        # caller turning down that clan's invitation. Then pid is whatever the script handed the
        # client, sent on unchanged (A6C510 looks the invitation up by its own pid instead), so
        # it is not what says whose invitation goes.
        7: ("RemoveClanInvite", [("in", "uint32", "pid"), ("in", "uint32", "clanId"),
                                 ("out", "uint32", "result")], None),
        # Accepting an invitation.
        8: ("JoinClan", [("in", "uint32", "clanId"), ("out", "uint32", "result")], None),
        # The caller's own pid to leave; anybody else's to kick them.
        9: ("LeaveClan", [("in", "uint32", "pid"), ("out", "uint32", "result")], None),
        11: ("ChangeClanRole", [("in", "uint32", "pid"), ("in", "uint32", "role"),
                                ("out", "uint32", "result")], None),
        12: ("ChangeClanRolePriv", [("in", "uint32", "role"), ("in", "uint32", "privileges"),
                                    ("out", "uint32", "result")], None),
        13: ("GetCurUserClan", [("out", "uint32", "clanId")], None),
        14: ("GetClanMembers", [("in", "uint32", "clanId"),
                                ("out", "qlist<ClanMember>", "members")], None),
        # hostData is the hosted session, serialized and hex-encoded by the client (8C8610) and
        # decoded again by whoever downloads it (8C8220); "" when the host leaves. state is 0 or
        # 1 with a session, 2 or 4 without one.
        15: ("SetClanRoom", [("in", "uint32", "clanId"), ("in", "string", "hostData"),
                             ("in", "byte", "room"), ("in", "byte", "state"),
                             ("in", "byte", "param2"), ("out", "uint32", "result")], None),
        16: ("DownloadClanRoomInfo", [("in", "byte", "first"), ("in", "byte", "count"),
                                      ("out", "qlist<ClanRoom>", "rooms")], None),
        # Room 0 is out of every room.
        17: ("JoinClanRoom", [("in", "byte", "room")], None),
        # The state is whether the host's session is full, 0 or 1.
        18: ("SetClanRoomState", [("in", "uint32", "clanId"), ("in", "byte", "room"),
                                  ("in", "byte", "state")], None),
        # The two u32 after the list land where the game script reads them (A6C190): the first
        # it starts at 0xFFFFFFFF, the second at 0.
        19: ("GetClanLeaderboard", [("in", "byte", "propertyId"), ("in", "uint32", "offset"),
                                    ("in", "uint32", "size"),
                                    ("out", "qlist<ClanLeaderboardEntry>", "entries"),
                                    ("out", "uint32", "ownRank"), ("out", "uint32", "total")],
             None),
        20: ("SetClanProperty", [("in", "uint32", "clanId"),
                                 ("in", "qlist<ClanPropertyChange>", "properties")], None),
        21: ("RemoveClanEvent", [("in", "uint32", "eventId"), ("out", "uint32", "result")],
             None),
        # Event id 0 makes a new event; any other changes that one.
        22: ("SetClanEvent", [("in", "ClanEvent", "event"), ("out", "uint32", "result")], None),
        23: ("GetClanEvents", [("in", "uint32", "clanId"), ("in", "byte", "eventType"),
                               ("in", "datetime", "since"), ("in", "datetime", "until"),
                               ("out", "qlist<ClanEvent>", "events")], None),
        24: ("ChangeEventType", [("in", "uint32", "eventId"), ("in", "byte", "eventType"),
                                 ("out", "uint32", "result")], None),
        # The client's TaskRDV_GetClanIdAndTags is two calls in a row: 26 turns players into
        # clan ids, one per player and 0 for none, and 27 turns those clans into tags.
        26: ("GetClanIdAndTags", [("in", "qlist<uint32>", "pids"),
                                  ("out", "qlist<uint32>", "clanIds")], None),
        27: ("GetClanTags", [("in", "qlist<uint32>", "clanIds"),
                             ("out", "qlist<string>", "tags")], None),
        # Clan 0: the invitations the caller has received. The caller's own clan: the ones it
        # has sent.
        28: ("GetClanInvitesWithNames", [("in", "uint32", "clanId"),
                                         ("out", "qlist<ClanInvite>", "invites"),
                                         ("out", "uint32", "result")], None),
    }

    def __init__(self, backend):
        super().__init__(backend)
        self.clans = backend.add_store(model.Clans(backend.data_path("clans.json")))

    # ----- the clan -----------------------------------------------------------------------------
    def CreateClan(self, ctx, name, tag, description):
        """The clan id lands in the client's current clan whatever the result, so a refusal
        answers the clan the player is in already (see above)."""
        pid = self._me(ctx)
        try:
            clan = self.clans.create(pid, name, tag, description)
        except model.Refused as e:
            return {"clanId": self.clans.clan_id_of(pid),
                    "result": self._refused(ctx, "create %r" % name, e)}
        self.log.info("%s founded clan %d %s [%s]", self._who(pid), clan.id, clan.name, clan.tag)
        return {"clanId": clan.id, "result": model.OK}

    def UpdateClan(self, ctx, tag, description, fields):
        pid = self._me(ctx)
        return self._do(ctx, "update the clan", self.clans.update, pid,
                        tag if fields & 1 else None, description if fields & 2 else None)

    def GetClanList(self, ctx, clanIds):
        """Clans that do not exist are left out; the client matches entries by id."""
        self._me(ctx)
        return {"clans": [self._info(clan) for clan in (self.clans.get(i) for i in clanIds)
                          if clan is not None]}

    def ChangeClanRolePriv(self, ctx, role, privileges):
        pid = self._me(ctx)
        return self._do(ctx, "role %d may 0x%04X" % (role, privileges & 0xFFFF),
                        self.clans.set_privileges, pid, role, privileges)

    def SetClanProperty(self, ctx, clanId, properties):
        """Declares nothing back, so a refusal is an RMC failure."""
        pid = self._me(ctx)
        changes = [(p["propertyId"], p["value"], p["op"]) for p in properties]
        try:
            clan = self.clans.set_properties(pid, clanId, changes)
        except model.Refused as e:
            raise RmcError(Errors.CORE_ACCESS_DENIED, e.why)
        self.log.info("%s: properties of %s now %s", self._who(pid), clan.name,
                      ", ".join("%d=%d" % (k, clan.properties[k])
                                for k, _, _ in changes) or "unchanged")
        return {}

    # ----- membership -------------------------------------------------------------------------
    def InviteToClan(self, ctx, pid, inviteType):
        me = self._me(ctx)
        if not self.backend.accounts.name_for(pid):
            return {"result": self._refused(ctx, "invite 0x%X" % pid, model.Refused(
                model.NO_SUCH_PLAYER, "nobody has that pid here"))}
        return self._do(ctx, "invite %s (type %d)" % (self._who(pid), inviteType),
                        self.clans.invite, me, pid)

    def InviteToClanWithName(self, ctx, name, inviteType):
        me = self._me(ctx)
        account = self.backend.accounts.get(name)
        if account is None:
            return {"result": self._refused(ctx, "invite %r" % name, model.Refused(
                model.NO_SUCH_PLAYER, "nobody of that name here"))}
        return self._do(ctx, "invite %s (type %d)" % (account.name, inviteType),
                        self.clans.invite, me, account.pid)

    def RemoveClanInvite(self, ctx, pid, clanId):
        me = self._me(ctx)
        if not clanId:
            return self._do(ctx, "take back the invitation to %s" % self._who(pid),
                            self.clans.cancel_invitation, me, pid)
        return self._do(ctx, "decline clan %d%s" % (clanId, " (pid 0x%X)" % pid
                                                    if pid not in (0, me) else ""),
                        self.clans.decline, me, clanId)

    def JoinClan(self, ctx, clanId):
        return self._do(ctx, "join clan %d" % clanId, self.clans.join, self._me(ctx), clanId)

    def LeaveClan(self, ctx, pid):
        me = self._me(ctx)
        try:
            clan, outcome = self.clans.leave(me, pid or me)
        except model.Refused as e:
            return {"result": self._refused(ctx, "take %s out" % self._who(pid or me), e)}
        if outcome == "kicked":
            self.log.info("%s removed %s from %s", self._who(me), self._who(pid), clan.name)
        elif outcome == "disbanded":
            self.log.info("%s left %s, the last one in: the clan is gone", self._who(me),
                          clan.name)
        else:
            self.log.info("%s left %s%s", self._who(me), clan.name,
                          ", which %s now leads" % self._who(clan.leader())
                          if outcome == "new leader" else "")
        return {"result": model.OK}

    def ChangeClanRole(self, ctx, pid, role):
        me = self._me(ctx)
        return self._do(ctx, "make %s a %d" % (self._who(pid), role), self.clans.change_role,
                        me, pid, role)

    def GetCurUserClan(self, ctx):
        """At every sign-in. 0 is no clan, which is no failure."""
        return {"clanId": self.clans.clan_id_of(self._me(ctx))}

    def GetClanMembers(self, ctx, clanId):
        """Clan 0 is the caller's own. A clan that is not there has nobody in it."""
        me = self._me(ctx)
        clan = self.clans.get(clanId or self.clans.clan_id_of(me))
        if clan is None:
            return {"members": []}
        return {"members": [self._member(m) for m in clan.members.values()]}

    def GetClanInvitesWithNames(self, ctx, clanId):
        me = self._me(ctx)
        if not clanId:
            invites = [self._invite(i, i.inviter) for i in self.clans.invitations_to(me)]
            return {"invites": invites, "result": model.OK}
        if clanId != self.clans.clan_id_of(me):
            return {"invites": [], "result": self._refused(
                ctx, "read clan %d's invitations" % clanId,
                model.Refused(model.NOT_ALLOWED, "only a clan's own members see its invitations"))}
        return {"invites": [self._invite(i, i.pid) for i in self.clans.invitations_from(clanId)],
                "result": model.OK}

    # ----- tags beside names --------------------------------------------------------------------
    def GetClanIdAndTags(self, ctx, pids):
        """One entry per pid, in the order asked, 0 for anybody in no clan or not known here."""
        return {"clanIds": [self.clans.clan_id_of(pid) for pid in pids]}

    def GetClanTags(self, ctx, clanIds):
        """One tag per clan asked about, "" for a clan that is gone."""
        tags = []
        for clan_id in clanIds:
            clan = self.clans.get(clan_id)
            tags.append(clan.tag if clan else "")
        return {"tags": tags}

    # ----- rooms ------------------------------------------------------------------------------
    def SetClanRoom(self, ctx, clanId, hostData, room, state, param2):
        me = self._me(ctx)
        try:
            hosted = self.clans.host_room(me, clanId, room, hostData, state)
        except model.Refused as e:
            return {"result": self._refused(ctx, "host room %d" % room, e)}
        if hosted is None:
            self.log.info("%s publishes room %d of clan %d with no session (state %d): closed"
                          " if it was theirs", self._who(me), room, self.clans.clan_id_of(me),
                          state)
        else:
            self.log.info("%s hosts room %d of clan %d (state %d, %d)", self._who(me), room,
                          self.clans.clan_id_of(me), state, param2)
        return {"result": model.OK}

    def DownloadClanRoomInfo(self, ctx, first, count):
        """The rooms somebody hosts, as they published them. Nobody in a clan has no rooms."""
        me = self._me(ctx)
        try:
            rooms = self.clans.rooms(me, first, count)
        except model.Refused:
            return {"rooms": []}
        return {"rooms": [{"room": r.number, "state": r.state, "data": r.data} for r in rooms]}

    def JoinClanRoom(self, ctx, room):
        """Declares nothing back, so a refusal - no clan, a room past 10 - is an RMC failure. A
        room nobody hosts yet is no refusal: that is where its host walks in first (A6A2E0, then
        SetClanRoom)."""
        me = self._me(ctx)
        try:
            self.clans.enter_room(me, room)
        except model.Refused as e:
            raise RmcError(Errors.CORE_INVALID_ARGUMENT, e.why)
        self.log.info("%s %s", self._who(me), "is in room %d" % room if room else "left the room")
        return {}

    def SetClanRoomState(self, ctx, clanId, room, state):
        me = self._me(ctx)
        try:
            self.clans.set_room_state(me, clanId, room, state)
        except model.Refused as e:
            raise RmcError(Errors.CORE_ACCESS_DENIED
                           if e.code == model.NOT_ALLOWED else Errors.CORE_INVALID_ARGUMENT, e.why)
        self.log.info("%s: room %d is %s", self._who(me), room, "full" if state else "not full")
        return {}

    # ----- the leaderboard --------------------------------------------------------------------
    def GetClanLeaderboard(self, ctx, propertyId, offset, size):
        """Every clan, best first by the property asked about, with all of its properties. The
        first u32 is where the caller's own clan stands, from 1 - 0xFFFFFFFF for none, the
        value the client starts it at; the second is how many clans the board has."""
        me = self._me(ctx)
        board = self.clans.leaderboard(propertyId)
        own = self.clans.clan_id_of(me)
        rank = next((n + 1 for n, clan in enumerate(board) if clan.id == own), 0xFFFFFFFF)
        page = board[offset:offset + size] if size else board[offset:]
        return {"entries": [{"clanId": clan.id, "name": clan.name,
                             "stats": [{"value": v, "propertyId": k}
                                       for k, v in sorted(clan.properties.items())]}
                            for clan in page],
                "ownRank": rank, "total": len(board)}

    # ----- the calendar -----------------------------------------------------------------------
    def SetClanEvent(self, ctx, event):
        me = self._me(ctx)
        try:
            done = self.clans.set_event(me, event["id"], event["clanId"], event["authorPid"],
                                        event["text"], event["eventType"], event["start"],
                                        event["end"])
        except model.Refused as e:
            return {"result": self._refused(ctx, "set event %d" % event["id"], e)}
        self.log.info("%s %s event %d of clan %d (type %d) %r", self._who(me),
                      "changed" if event["id"] else "added", done.id, done.clan, done.type,
                      done.text)
        return {"result": model.OK}

    def GetClanEvents(self, ctx, clanId, eventType, since, until):
        """Clan 0 is the caller's own."""
        me = self._me(ctx)
        clan_id = clanId or self.clans.clan_id_of(me)
        return {"events": [self._event(e) for e in
                           self.clans.events_of(clan_id, eventType, since, until)]}

    def RemoveClanEvent(self, ctx, eventId):
        me = self._me(ctx)
        return self._do(ctx, "remove event %d" % eventId, self.clans.remove_event, me, eventId)

    def ChangeEventType(self, ctx, eventId, eventType):
        me = self._me(ctx)
        return self._do(ctx, "make event %d type %d" % (eventId, eventType),
                        self.clans.change_event_type, me, eventId, eventType)

    # ----- replies ----------------------------------------------------------------------------
    def _info(self, clan) -> dict:
        shown = [clan.properties.get(n, 0) for n in range(model.SHOWN_PROPERTIES)]
        return {"id": clan.id, "name": clan.name, "tag": clan.tag,
                "description": clan.description, "flags": 0,
                "rolePrivileges": list(clan.privileges), "properties": shown, "extra": ""}

    def _member(self, member) -> dict:
        account = self.backend.accounts.by_pid.get(member.pid)
        online = self.backend.is_online(member.pid)
        seen = self.backend.now() if online else (account.last_seen if account
                                                  else member.joined)
        return {"pid": member.pid, "role": member.role, "joined": model.packed(member.joined),
                "lastSeen": model.packed(seen), "name": self._name(member.pid),
                "online": 1 if online else 0}

    def _invite(self, invitation, named) -> dict:
        return {"pid": invitation.pid, "clanId": invitation.clan,
                "inviterPid": invitation.inviter, "sent": model.packed(invitation.sent),
                "name": self._name(named)}

    @staticmethod
    def _event(event) -> dict:
        return {"id": event.id, "clanId": event.clan, "authorPid": event.author,
                "text": event.text, "eventType": event.type, "start": event.start,
                "end": event.end}

    # ----- inside -----------------------------------------------------------------------------
    def _me(self, ctx) -> int:
        if not ctx.client.pid:
            raise RmcError(Errors.RV_NOT_AUTHENTICATED, "the clan screen before logging in")
        return ctx.client.pid

    def _do(self, ctx, what, action, *args) -> dict:
        """One action that answers with a result code: 0, or why not."""
        try:
            action(*args)
        except model.Refused as e:
            return {"result": self._refused(ctx, what, e)}
        self.log.info("%s: %s", self._who(ctx.client.pid), what)
        return {"result": model.OK}

    def _refused(self, ctx, what, e) -> int:
        self.log.info("%s may not %s: %s (0x%X, %s)", self._who(ctx.client.pid), what, e.why,
                      e.code, model.CODES.get(e.code, "?"))
        return e.code

    def _name(self, pid: int) -> str:
        # The client copies at most 63 characters of a member's name.
        return self.backend.accounts.name_for(pid)[:63]

    def _who(self, pid: int) -> str:
        return self.backend.accounts.name_for(pid) or "0x%X" % pid
