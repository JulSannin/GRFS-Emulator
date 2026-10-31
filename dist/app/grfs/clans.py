"""Clans: who is in which clan, with what rank, and what the clan has planned.

Future Soldier signs every player in with ClansProtocol 13 GetCurUserClan, and from then on the
clan screen is built from what this keeps. A player is in one clan or none - the client holds a
single current clan id, and 0 in it means "no clan" - so this keeps one clan per player and
refuses a second.

A rank is a role, a small number where lower outranks higher:

    0  the leader - one per clan; nobody can change what the leader may do
    1, 2, 3  the other ranks; a new member starts at 3, the bottom

Every role carries a 16-bit privilege mask that the leader sets (ChangeClanRolePriv) and the
client reads back in the clan info. What each bit allows is the client's business: nothing in
the binary names them, so they are kept and returned, never enforced. What is enforced here is
rank, because a server that let anybody act on anybody would let a recruit disband a clan:

* only the leader changes the clan itself - its tag, its description, what each role may do;
* a member acts only on members ranked below them - promote, demote, kick - and can only hand
  out ranks below their own. Making somebody leader is the leader's alone, and hands over the
  clan: the old leader becomes a rank 1;
* anybody in the clan may invite, and cancel the clan's invitations;
* a leader who leaves passes the clan to the best-ranked member who has been in it longest; the
  last one out disbands it, and its name is free again.

Refusals are answered as a result code, the clans protocol's own way of failing - every one of
its calls that can fail carries a u32 for it, which the client starts at 2 and posts to the
screen as an error whenever it is not 0. The client understands exactly one code itself:

    0x324  "not in a clan" - it clears its own clan id and shows the screen for no clan
           (completion A6BBC0)

so that is what a player who is in no clan is told, and only them: answering it to somebody who
is in a clan would make their game forget it. The other codes are chosen here; the client shows
any of them as a plain failure.

Rooms are the clan's lobbies, numbered 1 to 10. A member first walks into one (JoinClanRoom - the
room is usually empty then, since that is how a host gets there), then publishes the session it
hosts there with SetClanRoom; the others download it and join that session. The state byte that
comes with the room is the client's: what the clan screen does with it is that 1 makes the room
unjoinable (A69850), and SetClanRoomState sets it from whether the session is full (A61870 sends 0
when a full session gets a free slot, A62320 sends 1). A host who walks out, or publishes the room
again with no session - which is what the client does when its host leaves (A6A900, A6A590) - closes
it. Rooms are kept in memory only: what they point at is a game that is gone once the server
restarts.

Everything else - clans, members, invitations, properties, events - is in one JSON file, saved
after every change.
"""

import datetime
import json
import os
import time

from . import log as logs
from .rmc.stream import pack_datetime

LEADER = 0
NEWCOMER = 3
ROLES = (0, 1, 2, 3)

# Until the leader says otherwise every role may do everything the client offers it; the server
# only holds the ranks apart.
DEFAULT_PRIVILEGES = (0xFFFF, 0xFFFF, 0xFFFF, 0xFFFF)

# The lengths the client's own CreateClan checks before it sends anything (it answers 0x2710
# itself for longer), and its limit on an event's text: SetClanEvent converts the text into a
# 256-byte UTF-8 buffer (A6BFC6), so 255 bytes of it at most.
MAX_NAME = 30
MAX_TAG = 3
MAX_DESCRIPTION = 80
MAX_EVENT_TEXT = 255

# ClanInfo carries the clan's properties as a list indexed by property id, and the client keeps
# the first 21 of it.
SHOWN_PROPERTIES = 21

# A clan has ten rooms, numbered from 1; DownloadClanRoomInfo asks for (0, 10).
ROOMS = 10

# Result codes. 0 and 0x324 are the client's; the rest are this server's.
OK = 0
NOT_IN_A_CLAN = 0x324
ALREADY_IN_A_CLAN = 0x325
NAME_TAKEN = 0x326
NO_SUCH_PLAYER = 0x327
NO_SUCH_CLAN = 0x328
NOT_ALLOWED = 0x329
NOT_INVITED = 0x32A
INVALID = 0x32B
NO_SUCH_EVENT = 0x32C

CODES = {OK: "ok", NOT_IN_A_CLAN: "not in a clan", ALREADY_IN_A_CLAN: "already in a clan",
         NAME_TAKEN: "name taken", NO_SUCH_PLAYER: "no such player", NO_SUCH_CLAN: "no such clan",
         NOT_ALLOWED: "not allowed", NOT_INVITED: "not invited", INVALID: "invalid",
         NO_SUCH_EVENT: "no such event"}

# How SetClanProperty applies a value - the same operation codes as the statistics protocol,
# which is the same Ubisoft layer: 0 add, 1 subtract, 2 set, 3 keep the larger, 4 the smaller.
ADD, SUB, SET, MAX, MIN = range(5)


class Refused(Exception):
    """An action this player may not take; `code` is what the client is answered."""

    def __init__(self, code: int, why: str):
        super().__init__(why)
        self.code = code
        self.why = why


def _now() -> float:
    return time.time()


def packed(t: float) -> int:
    """A stored time as the protocol's datetime."""
    return pack_datetime(datetime.datetime.fromtimestamp(t or 0, datetime.timezone.utc))


def _u32(value) -> int:
    return int(value) & 0xFFFFFFFF


def apply_property(old, value: int, op: int) -> int:
    """What a property holds after one SetClanProperty entry, from `old` (None when it was never
    set: adding to nothing starts from 0, a bound on nothing is the value). An unknown op sets."""
    value = _u32(value)
    if old is None:
        return _u32(-value) if op == SUB else value
    if op == ADD:
        return _u32(old + value)
    if op == SUB:
        return _u32(old - value)
    if op == MAX:
        return max(old, value)
    if op == MIN:
        return min(old, value)
    return value


class Member:
    __slots__ = ("pid", "role", "joined")

    def __init__(self, pid: int, role: int, joined: float):
        self.pid = pid
        self.role = role
        self.joined = joined


class Invitation:
    __slots__ = ("clan", "pid", "inviter", "sent")

    def __init__(self, clan: int, pid: int, inviter: int, sent: float):
        self.clan = clan
        self.pid = pid
        self.inviter = inviter
        self.sent = sent


class Event:
    """A date on the clan's calendar. Start and end are kept as the client sent them - packed
    datetimes, which compare in time order as plain numbers."""

    __slots__ = ("id", "clan", "author", "text", "type", "start", "end")

    def __init__(self, id: int, clan: int, author: int, text: str, type: int, start: int,
                 end: int):
        self.id = id
        self.clan = clan
        self.author = author
        self.text = text
        self.type = type
        self.start = start
        self.end = end


class Room:
    __slots__ = ("number", "host", "state", "data", "inside")

    def __init__(self, number: int, host: int, state: int, data: str):
        self.number = number
        self.host = host
        self.state = state
        self.data = data
        self.inside = set()


class Clan:
    __slots__ = ("id", "name", "tag", "description", "created", "members", "privileges",
                 "properties", "rooms")

    def __init__(self, id: int, name: str, tag: str, description: str, created: float):
        self.id = id
        self.name = name
        self.tag = tag
        self.description = description
        self.created = created
        self.members = {}                       # pid -> Member, in the order they joined
        self.privileges = list(DEFAULT_PRIVILEGES)
        self.properties = {}                    # property id -> u32
        self.rooms = {}                         # room number -> Room; never saved

    def leader(self) -> int:
        for member in self.members.values():
            if member.role == LEADER:
                return member.pid
        return 0

    def successor(self, leaving: int) -> Member:
        """Who takes over from a leader who goes: best rank, then longest in the clan."""
        rest = [m for m in self.members.values() if m.pid != leaving]
        return min(rest, key=lambda m: (m.role, m.joined)) if rest else None


class Clans:
    """Every clan, in memory, and in one JSON file when there is a path."""

    def __init__(self, path: str = None):
        self.path = path
        self.log = logs.get("clans")
        self.clans = {}                         # clan id -> Clan
        self.invitations = []                   # Invitation, oldest first
        self.events = {}                        # event id -> Event
        self.next_clan = 1
        self.next_event = 1
        self.load()

    # ----- looking up -----------------------------------------------------------------------
    def get(self, clan_id: int):
        return self.clans.get(clan_id)

    def clan_of(self, pid: int):
        """The clan this player is in, or None."""
        for clan in self.clans.values():
            if pid in clan.members:
                return clan
        return None

    def clan_id_of(self, pid: int) -> int:
        clan = self.clan_of(pid)
        return clan.id if clan else 0

    def by_name(self, name: str):
        key = (name or "").strip().lower()
        for clan in self.clans.values():
            if clan.name.lower() == key:
                return clan
        return None

    def invitations_to(self, pid: int) -> list:
        return [i for i in self.invitations if i.pid == pid]

    def invitations_from(self, clan_id: int) -> list:
        return [i for i in self.invitations if i.clan == clan_id]

    def events_of(self, clan_id: int, type: int = 0, since: int = 0, until: int = 0) -> list:
        """The clan's events of this type (0 any) that are on at some point between the two
        times (0 for no bound), earliest first."""
        found = [e for e in self.events.values() if e.clan == clan_id
                 and (not type or e.type == type)
                 and (not until or e.start <= until)
                 and (not since or (e.end or e.start) >= since)]
        return sorted(found, key=lambda e: (e.start, e.id))

    def leaderboard(self, property_id: int) -> list:
        """Every clan, best first by one property, ties by the older clan."""
        return sorted(self.clans.values(),
                      key=lambda c: (-c.properties.get(property_id, 0), c.id))

    # ----- the clan itself --------------------------------------------------------------------
    def create(self, pid: int, name: str, tag: str, description: str) -> Clan:
        name, tag, description = (name or "").strip(), (tag or "").strip(), description or ""
        self._member_of_none(pid)
        if not name or len(name) > MAX_NAME or len(tag) > MAX_TAG \
                or len(description) > MAX_DESCRIPTION:
            raise Refused(INVALID, "a name of 1 to %d characters, a tag of up to %d and a"
                          " description of up to %d" % (MAX_NAME, MAX_TAG, MAX_DESCRIPTION))
        if self.by_name(name) is not None:
            raise Refused(NAME_TAKEN, "there is a clan called %r already" % name)
        clan = Clan(self.next_clan, name, tag, description, _now())
        self.next_clan += 1
        clan.members[pid] = Member(pid, LEADER, clan.created)
        self.clans[clan.id] = clan
        self._drop_invitations(pid=pid)
        self._changed()
        return clan

    def update(self, pid: int, tag: str = None, description: str = None) -> Clan:
        clan = self._leader_of(pid)
        if tag is not None:
            tag = tag.strip()
            if len(tag) > MAX_TAG:
                raise Refused(INVALID, "a tag of up to %d characters" % MAX_TAG)
            clan.tag = tag
        if description is not None:
            if len(description) > MAX_DESCRIPTION:
                raise Refused(INVALID, "a description of up to %d characters" % MAX_DESCRIPTION)
            clan.description = description
        self._changed()
        return clan

    def set_privileges(self, pid: int, role: int, mask: int) -> Clan:
        clan = self._leader_of(pid)
        if role not in ROLES or role == LEADER:
            raise Refused(INVALID, "the leader's privileges are not set, and there are roles"
                          " 1 to 3 besides")
        clan.privileges[role] = mask & 0xFFFF
        self._changed()
        return clan

    def set_properties(self, pid: int, clan_id: int, changes) -> Clan:
        """changes: (property id, value, op). Any member may - the game adds to its clan's
        totals from whoever played."""
        clan = self._in_clan(pid)
        if clan_id and clan_id != clan.id:
            raise Refused(NOT_ALLOWED, "properties of clan %d, not its own" % clan_id)
        for property_id, value, op in changes:
            clan.properties[property_id] = apply_property(clan.properties.get(property_id),
                                                          value, op)
        self._changed()
        return clan

    # ----- membership -------------------------------------------------------------------------
    def invite(self, pid: int, invitee: int) -> Invitation:
        clan = self._in_clan(pid)
        if invitee == pid or invitee in clan.members:
            raise Refused(INVALID, "already in this clan")
        if self.clan_of(invitee) is not None:
            raise Refused(ALREADY_IN_A_CLAN, "in another clan")
        for invitation in self.invitations:
            if invitation.clan == clan.id and invitation.pid == invitee:
                return invitation               # asked twice: once is enough
        invitation = Invitation(clan.id, invitee, pid, _now())
        self.invitations.append(invitation)
        self._changed()
        return invitation

    def cancel_invitation(self, pid: int, invitee: int) -> Invitation:
        """The clan takes back an invitation it sent."""
        clan = self._in_clan(pid)
        return self._take_invitation(clan.id, invitee)

    def decline(self, pid: int, clan_id: int) -> Invitation:
        """The player turns an invitation down."""
        return self._take_invitation(clan_id, pid)

    def join(self, pid: int, clan_id: int) -> Clan:
        """Accept an invitation: the only way into a clan but founding one."""
        self._member_of_none(pid)
        clan = self.clans.get(clan_id)
        if clan is None:
            raise Refused(NO_SUCH_CLAN, "clan %d" % clan_id)
        if not any(i.clan == clan_id and i.pid == pid for i in self.invitations):
            raise Refused(NOT_INVITED, "not invited to %s" % clan.name)
        clan.members[pid] = Member(pid, NEWCOMER, _now())
        # In a clan, every other invitation is one that can only be refused.
        self._drop_invitations(pid=pid)
        self._changed()
        return clan

    def leave(self, pid: int, who: int):
        """`pid` takes `who` out of their clan: themselves, or a member ranked below them.
        Returns (clan, what became of it): "left", "kicked", "new leader" or "disbanded"."""
        clan = self._in_clan(pid)
        if who != pid:
            target = clan.members.get(who)
            if target is None:
                raise Refused(NO_SUCH_PLAYER, "not in %s" % clan.name)
            if clan.members[pid].role >= target.role:
                raise Refused(NOT_ALLOWED, "only a member ranked above can remove another")
            del clan.members[who]
            self._leave_rooms(clan, who)
            self._changed()
            return clan, "kicked"
        outcome = "left"
        if clan.members[pid].role == LEADER:
            heir = clan.successor(pid)
            if heir is None:
                self._disband(clan)
                self._changed()
                return clan, "disbanded"
            heir.role = LEADER
            outcome = "new leader"
        del clan.members[pid]
        self._leave_rooms(clan, pid)
        self._changed()
        return clan, outcome

    def change_role(self, pid: int, who: int, role: int) -> Clan:
        clan = self._in_clan(pid)
        if role not in ROLES:
            raise Refused(INVALID, "roles are 0 to 3")
        if who == pid:
            raise Refused(INVALID, "nobody sets their own role")
        target = clan.members.get(who)
        if target is None:
            raise Refused(NO_SUCH_PLAYER, "not in %s" % clan.name)
        mine = clan.members[pid].role
        if role == LEADER:
            if mine != LEADER:
                raise Refused(NOT_ALLOWED, "only the leader hands the clan over")
            target.role = LEADER
            clan.members[pid].role = 1
        else:
            if mine >= target.role or mine >= role:
                raise Refused(NOT_ALLOWED, "rank %d cannot make rank %d a %d"
                              % (mine, target.role, role))
            target.role = role
        self._changed()
        return clan

    # ----- the calendar -----------------------------------------------------------------------
    def set_event(self, pid: int, event_id: int, clan_id: int, author: int, text: str,
                  type: int, start: int, end: int) -> Event:
        """Event 0 is a new one; any other is changed, by its author or a rank 0 or 1."""
        clan = self._in_clan(pid)
        if clan_id and clan_id != clan.id:
            raise Refused(NOT_ALLOWED, "an event for clan %d, not its own" % clan_id)
        text = text or ""
        if not type or len(text.encode("utf-8")) > MAX_EVENT_TEXT:
            raise Refused(INVALID, "an event has a type and up to %d bytes of text"
                          % MAX_EVENT_TEXT)
        if event_id:
            event = self._own_event(pid, clan, event_id)
            event.text, event.type, event.start, event.end = text, type, start, end
        else:
            event = Event(self.next_event, clan.id, author or pid, text, type, start, end)
            self.next_event += 1
            self.events[event.id] = event
        self._changed()
        return event

    def remove_event(self, pid: int, event_id: int) -> Event:
        clan = self._in_clan(pid)
        event = self._own_event(pid, clan, event_id)
        del self.events[event.id]
        self._changed()
        return event

    def change_event_type(self, pid: int, event_id: int, type: int) -> Event:
        clan = self._in_clan(pid)
        if not type:
            raise Refused(INVALID, "an event has a type")
        event = self._own_event(pid, clan, event_id)
        event.type = type
        self._changed()
        return event

    # ----- rooms ------------------------------------------------------------------------------
    def host_room(self, pid: int, clan_id: int, number: int, data: str, state: int):
        """Publish the session hosted in a room, or with no session close the room. Returns the
        room, None once closed."""
        clan = self._in_clan(pid)
        if clan_id and clan_id != clan.id:
            raise Refused(NOT_ALLOWED, "a room of clan %d, not its own" % clan_id)
        if not 1 <= number <= ROOMS:
            raise Refused(INVALID, "rooms are 1 to %d" % ROOMS)
        room = clan.rooms.get(number)
        theirs = room is not None and room.host != pid and room.host in clan.members
        if not data:
            # The host going: whoever else says so has nothing here to close.
            if room is not None and not theirs:
                del clan.rooms[number]
            return None
        if theirs:
            raise Refused(NOT_ALLOWED, "room %d is hosted by somebody else" % number)
        if room is None or room.host != pid:
            room = clan.rooms[number] = Room(number, pid, state, data)
        room.state, room.data = state, data
        room.inside.add(pid)
        return room

    def set_room_state(self, pid: int, clan_id: int, number: int, state: int) -> Room:
        """The host says whether its session is full; the room stays open either way."""
        clan = self._in_clan(pid)
        room = clan.rooms.get(number)
        if room is None or (clan_id and clan_id != clan.id):
            raise Refused(INVALID, "no room %d" % number)
        if room.host != pid:
            raise Refused(NOT_ALLOWED, "only its host changes room %d" % number)
        room.state = state
        return room

    def enter_room(self, pid: int, number: int):
        """Go into a room - hosted or not yet, which is how its host gets there - or with 0 out
        of every room. A host who walks out of a room closes it."""
        clan = self._in_clan(pid)
        if number and not 1 <= number <= ROOMS:
            raise Refused(INVALID, "rooms are 1 to %d" % ROOMS)
        self._leave_rooms(clan, pid, but=number)
        room = clan.rooms.get(number)
        if room is not None:
            room.inside.add(pid)
        return room

    def rooms(self, pid: int, first: int, count: int) -> list:
        clan = self._in_clan(pid)
        wanted = range(first + 1, first + 1 + count)
        return [clan.rooms[n] for n in sorted(clan.rooms) if n in wanted]

    # ----- inside -----------------------------------------------------------------------------
    def _in_clan(self, pid: int) -> Clan:
        clan = self.clan_of(pid)
        if clan is None:
            raise Refused(NOT_IN_A_CLAN, "in no clan")
        return clan

    def _member_of_none(self, pid: int) -> None:
        clan = self.clan_of(pid)
        if clan is not None:
            raise Refused(ALREADY_IN_A_CLAN, "already in %s" % clan.name)

    def _leader_of(self, pid: int) -> Clan:
        clan = self._in_clan(pid)
        if clan.members[pid].role != LEADER:
            raise Refused(NOT_ALLOWED, "only the leader of %s may" % clan.name)
        return clan

    def _own_event(self, pid: int, clan: Clan, event_id: int) -> Event:
        event = self.events.get(event_id)
        if event is None or event.clan != clan.id:
            raise Refused(NO_SUCH_EVENT, "no event %d in %s" % (event_id, clan.name))
        if event.author != pid and clan.members[pid].role > 1:
            raise Refused(NOT_ALLOWED, "only its author or rank 0 or 1 changes an event")
        return event

    def _take_invitation(self, clan_id: int, pid: int) -> Invitation:
        for invitation in self.invitations:
            if invitation.clan == clan_id and invitation.pid == pid:
                self.invitations.remove(invitation)
                self._changed()
                return invitation
        raise Refused(NOT_INVITED, "no invitation from clan %d to 0x%X" % (clan_id, pid))

    def _drop_invitations(self, pid: int = None, clan: int = None) -> None:
        self.invitations = [i for i in self.invitations
                            if not (pid is not None and i.pid == pid)
                            and not (clan is not None and i.clan == clan)]

    def _leave_rooms(self, clan: Clan, pid: int, but: int = 0) -> None:
        for number, room in list(clan.rooms.items()):
            if number == but:
                continue
            room.inside.discard(pid)
            if room.host == pid:
                del clan.rooms[number]

    def _disband(self, clan: Clan) -> None:
        del self.clans[clan.id]
        self._drop_invitations(clan=clan.id)
        for event_id in [e.id for e in self.events.values() if e.clan == clan.id]:
            del self.events[event_id]

    def _changed(self) -> None:
        self.save()

    def __len__(self):
        return len(self.clans)

    # ----- storage ----------------------------------------------------------------------------
    def load(self) -> None:
        if not self.path or not os.path.exists(self.path):
            return
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            clans = data["clans"]
            if not isinstance(clans, list):
                raise ValueError("'clans' is not a list")
        except (OSError, ValueError, KeyError, TypeError) as e:
            # Starting empty and then saving would disband every clan for good. The file goes
            # aside first, so it can be mended.
            aside = "%s.bad-%d" % (self.path, int(time.time()))
            try:
                os.replace(self.path, aside)
            except OSError:
                aside = None
            self.log.error("cannot read %s (%s) - starting without clans%s", self.path, e,
                           (", the file is kept as %s" % aside) if aside else "")
            return
        skipped = 0
        seen = set()                            # one clan per player, whatever the file says
        for row in clans:
            try:
                clan = Clan(int(row["id"]), str(row["name"]), str(row.get("tag", "")),
                            str(row.get("description", "")), float(row.get("created", 0)))
                for m in row["members"]:
                    pid = int(m["pid"])
                    if pid not in seen:
                        seen.add(pid)
                        clan.members[pid] = Member(pid, int(m["role"]) & 3,
                                                   float(m.get("joined", clan.created)))
                privileges = [int(p) & 0xFFFF for p in row.get("privileges", [])]
                clan.privileges = (privileges
                                   + list(DEFAULT_PRIVILEGES[len(privileges):]))[:len(ROLES)]
                clan.properties = {int(k) & 0xFF: _u32(v)
                                   for k, v in row.get("properties", {}).items()}
            except (KeyError, TypeError, ValueError):
                skipped += 1
                continue
            if not clan.members:
                skipped += 1
                continue
            if not clan.leader():
                clan.successor(0).role = LEADER
            self.clans[clan.id] = clan
        for row in data.get("invitations", []):
            try:
                invitation = Invitation(int(row["clan"]), int(row["pid"]),
                                        int(row.get("inviter", 0)), float(row.get("sent", 0)))
            except (KeyError, TypeError, ValueError):
                skipped += 1
                continue
            if invitation.clan in self.clans:
                self.invitations.append(invitation)
        for row in data.get("events", []):
            try:
                event = Event(int(row["id"]), int(row["clan"]), int(row.get("author", 0)),
                              str(row.get("text", "")), int(row["type"]) & 0xFF,
                              int(row.get("start", 0)), int(row.get("end", 0)))
            except (KeyError, TypeError, ValueError):
                skipped += 1
                continue
            if event.clan in self.clans:
                self.events[event.id] = event
        self.next_clan = max([int(data.get("next_clan", 1))] + [c + 1 for c in self.clans])
        self.next_event = max([int(data.get("next_event", 1))] + [e + 1 for e in self.events])
        if skipped:
            self.log.warning("%s: %d unreadable entr(y/ies) left out", self.path, skipped)
        self.log.info("%d clan(s) from %s", len(self.clans), self.path)

    def save(self) -> None:
        if not self.path:
            return
        data = {
            # Ids are never given out twice, even after the clan that had one is gone: an id the
            # client still holds must not come to mean somebody else's clan.
            "next_clan": self.next_clan,
            "next_event": self.next_event,
            "clans": [{"id": c.id, "name": c.name, "tag": c.tag, "description": c.description,
                       "created": c.created,
                       "members": [{"pid": m.pid, "role": m.role, "joined": m.joined}
                                   for m in c.members.values()],
                       "privileges": list(c.privileges),
                       "properties": {str(k): v for k, v in sorted(c.properties.items())}}
                      for c in sorted(self.clans.values(), key=lambda c: c.id)],
            "invitations": [{"clan": i.clan, "pid": i.pid, "inviter": i.inviter, "sent": i.sent}
                            for i in self.invitations],
            "events": [{"id": e.id, "clan": e.clan, "author": e.author, "text": e.text,
                        "type": e.type, "start": e.start, "end": e.end}
                       for e in sorted(self.events.values(), key=lambda e: e.id)],
        }
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=1)
            os.replace(tmp, self.path)
        except OSError as e:
            self.log.error("cannot write %s: %s", self.path, e)
