"""Game sessions: the squad lobby and the co-op match, as the server keeps them.

Both are the same thing to the client - a GameSession of type 1 with a list of numbered
attributes and two lists of participants - and both live only as long as the games in them, so
nothing here is written to disk. The protocol is in services.game_session; this is the state and
the rules, with no networking in it.

Rules the client depends on, each read out of its code (see the module docstring of
services.game_session for where):

* Attributes 3 and 4 are the public and private *capacities*; 5 and 6 are the slots *used*. The
  client sends 5 = 6 = 0 when it creates a session and never again, so they belong to the server
  and follow the participant lists. Every result must carry all four: the parser reads them
  without a null check, and a session without one crashes whoever searched for it.
* An update carries only the attributes that changed - often a single one. It is merged by id.
  Replacing the set with it once dropped 3 to 6 and would crash every searcher.
* The host adds every member, itself included, after they have connected to it. A join asks
  nothing of the server, so the participant lists only ever change on the host's word, or when a
  player leaves.
* A host that quits *abandons* the session, and the member who takes over then *migrates* it
  under the same key. So a session outlives its host for as long as anybody is still in it.
* A game that crashes and is started again logs in under the same pid from a new connection,
  and is in no session at all. What the old one hosted is abandoned at that point, not ten
  minutes later when its connection is forgotten; until then its sessions would be offered with
  the new game's URLs, and whoever joined would reach a game that was hosting nothing.
"""

from .rmc.stream import pack_datetime

# ----- attribute ids ---------------------------------------------------------------------------
QUERY_ID = 0            # only in a query: which query it is
PUBLIC_SLOTS = 3        # capacities, the client's
PRIVATE_SLOTS = 4
USED_PUBLIC = 5         # used, the server's
USED_PRIVATE = 6
INVITE_ONLY = 7         # 1 when the session was created with no public slots: a squad
FREE_PUBLIC = 0x32      # 3 - 5 and 4 - 6, worked out by the client and never sent
FREE_PRIVATE = 0x33
ACCESSIBILITY = 0x66    # bit 1 hides the session from friends
CLAN = 0x6C             # the searcher's clan in a quick-match query, 0 on the host's side
QUICK_MATCH_A = 0x6E    # sent in a quick-match query as 5 and 0; never by any host
QUICK_MATCH_B = 0x6F

# What a client may never set: the query id, the used counts and what follows from them.
SERVER_OWNED = frozenset((QUERY_ID, USED_PUBLIC, USED_PRIVATE, FREE_PUBLIC, FREE_PRIVATE))

# Query parameters that are not "the session must have this value". 3 is a slot requirement and
# is handled on its own. The quick-match searcher sends its own clan id as 0x6C while the host
# writes 0 there, so equality would only ever match a player without a clan; 0x6E and 0x6F have no
# counterpart on the host's side at all.
NOT_EQUALITY = frozenset((QUERY_ID, PUBLIC_SLOTS, CLAN, QUICK_MATCH_A, QUICK_MATCH_B))

# The two queries the game makes. 2 is the browser and custom match (param 3 = the size of the
# searcher's party, only when it has one); 3 is quick match (param 3 = the lobby size the searcher
# picked, which the host doubled into its capacity).
QUERY_BROWSE = 2
QUERY_QUICK_MATCH = 3
QUERIES = (QUERY_BROWSE, QUERY_QUICK_MATCH)

# The client probes every result it is given, twice with five seconds each for one that does not
# answer, before it shows anything. A long list is a long wait.
SEARCH_LIMIT = 20

# The client always asks for invitations with ResultRange(0, 25) and treats the *last* one as the
# one that just arrived. Keeping no more than that per player keeps the newest inside the range.
INVITATIONS_KEPT = 25

# The first session id handed out. Anything but 0, which the client would take for "none".
FIRST_SESSION_ID = 1


class Session:
    __slots__ = ("type_id", "id", "owner", "attributes", "public", "private", "created")

    def __init__(self, type_id: int, session_id: int, owner: int, attributes: dict,
                 created: float = 0.0):
        self.type_id = type_id
        self.id = session_id
        # The host's pid, or 0 between the host abandoning it and somebody migrating it.
        self.owner = owner
        self.attributes = attributes
        self.public = []
        self.private = []
        self.created = created

    @property
    def key(self) -> dict:
        return {"m_typeID": self.type_id, "m_sessionID": self.id}

    def participants(self) -> list:
        return self.public + self.private

    def has(self, pid: int) -> bool:
        return pid in self.public or pid in self.private

    def involves(self, pid: int) -> bool:
        """In it, or hosting it - the host is a participant only once it has added itself."""
        return pid == self.owner or self.has(pid)

    def capacity(self) -> tuple:
        return (max(0, self.attributes.get(PUBLIC_SLOTS, 0)),
                max(0, self.attributes.get(PRIVATE_SLOTS, 0)))

    def used(self) -> tuple:
        """Attributes 5 and 6 as the client is told them.

        The host puts an invited friend in the private list even when the session has no private
        slots - both match creates send 4 = 0 - and the friend is in the game all the same. So
        private members beyond the private capacity are counted against the public slots, and
        neither count goes past its capacity: the client works out the free slots as 3 - 5 and
        4 - 6, and a negative number of free slots is not something it was written to see.
        """
        public, private = self.capacity()
        over = max(0, len(self.private) - private)
        return min(public, len(self.public) + over), min(private, len(self.private))

    def free(self) -> tuple:
        used_public, used_private = self.used()
        public, private = self.capacity()
        return public - used_public, private - used_private

    def attribute_list(self) -> list:
        """Every attribute as a Property list, 3 to 6 always among them."""
        values = dict(self.attributes)
        values.setdefault(PUBLIC_SLOTS, 0)
        values.setdefault(PRIVATE_SLOTS, 0)
        values[USED_PUBLIC], values[USED_PRIVATE] = self.used()
        return [{"m_ID": aid, "m_value": values[aid]} for aid in sorted(values)]

    def __repr__(self):
        return "<Session %d/%d host 0x%X %d+%d>" % (self.type_id, self.id, self.owner,
                                                    len(self.public), len(self.private))


class Invitation:
    __slots__ = ("recipient", "sender", "type_id", "session_id", "message", "created")

    def __init__(self, recipient: int, sender: int, type_id: int, session_id: int,
                 message: str, created: int):
        self.recipient = recipient
        self.sender = sender
        self.type_id = type_id
        self.session_id = session_id
        self.message = message
        self.created = created          # a packed DateTime, as it goes on the wire

    @property
    def key(self) -> dict:
        return {"m_typeID": self.type_id, "m_sessionID": self.session_id}

    def received(self) -> dict:
        return {"m_sessionKey": self.key, "m_senderPID": self.sender,
                "m_message": self.message, "m_creationTime": self.created}

    def sent(self) -> dict:
        return {"m_sessionKey": self.key, "m_recipientPID": self.recipient,
                "m_message": self.message, "m_creationTime": self.created}

    def __repr__(self):
        return "<Invitation 0x%X -> 0x%X to %d>" % (self.sender, self.recipient,
                                                    self.session_id)


class Sessions:
    """Every open session, every invitation not yet answered, and the URLs players registered.

    Sessions are found by id alone. The id is unique across types, and the key the client sends
    is not always whole: when an invitation arrives by notification the client builds the key
    from the notification and never fills in its type, so the GetSession and AcceptInvitation
    that follow carry whatever was in that memory.
    """

    def __init__(self, now=None):
        self.by_id = {}
        self.invitations = []
        # pid -> station URLs from GameSession's own RegisterURLs.
        self.urls = {}
        # pid -> whatever stands for the running game it last called from (the protocol layer
        # passes its connection record). A different one means the game was restarted.
        self.games = {}
        self._next_id = FIRST_SESSION_ID
        self._now = now or (lambda: 0.0)

    def __len__(self):
        return len(self.by_id)

    def __iter__(self):
        return iter(list(self.by_id.values()))

    # ----- sessions ----------------------------------------------------------------------------
    def create(self, owner: int, type_id: int, attributes) -> Session:
        """A new session hosted by `owner`. Attributes are kept as sent, except the ones the
        server owns; 3 and 4 are filled in with 0 if the client left them out."""
        values = {}
        for aid, value in _pairs(attributes):
            if aid not in SERVER_OWNED:
                values[aid] = value
        values.setdefault(PUBLIC_SLOTS, 0)
        values.setdefault(PRIVATE_SLOTS, 0)
        session = Session(type_id, self._next_id, owner, values, self._now())
        self._next_id += 1
        self.by_id[session.id] = session
        return session

    def get(self, session_id: int):
        return self.by_id.get(session_id)

    def update(self, session: Session, attributes) -> list:
        """Merge by id. Returns the ids that were refused because the server owns them."""
        refused = []
        for aid, value in _pairs(attributes):
            if aid in SERVER_OWNED:
                refused.append(aid)
            else:
                session.attributes[aid] = value
        return refused

    def delete(self, session: Session) -> None:
        self.by_id.pop(session.id, None)
        self.invitations = [i for i in self.invitations if i.session_id != session.id]

    def add(self, session: Session, public=(), private=()) -> list:
        """Never refuses and never drops anyone: the host has already let them in. A pid that is
        already in either list stays where it is."""
        added = []
        for pids, into in ((public, session.public), (private, session.private)):
            for pid in pids or ():
                if pid and not session.has(pid):
                    into.append(pid)
                    added.append(pid)
        return added

    def remove(self, session: Session, pids) -> list:
        removed = []
        for pid in pids or ():
            for where in (session.public, session.private):
                if pid in where:
                    where.remove(pid)
                    removed.append(pid)
        return removed

    def leave(self, session: Session, pid: int) -> bool:
        """`pid` goes. A host leaving leaves the session without one, for whoever migrates it.
        Returns whether that emptied the session, which is then gone."""
        self.remove(session, [pid])
        if session.owner == pid:
            session.owner = 0
        if not session.owner and not session.participants():
            self.delete(session)
            return True
        return False

    def migrate(self, session: Session, pid: int) -> None:
        """`pid` hosts it now. Attributes and participants stay; the new host compares the list
        with its own and adds or removes the difference itself."""
        session.owner = pid

    def sessions_of(self, pid: int) -> list:
        return [s for s in self.by_id.values() if s.involves(pid)]

    def seen(self, pid: int, game) -> list:
        """`pid` is calling from `game`. If that is not the game it called from before, the
        earlier one was restarted, and a restarted game is in no session: it leaves each one
        (see restarted). Returns the sessions it left."""
        before = self.games.get(pid)
        self.games[pid] = game
        if before is None or before is game:
            return []
        return self.restarted(pid)

    def players(self) -> list:
        """Every pid whose game has called, for checking which of them were restarted since."""
        return list(self.games)

    def restarted(self, pid: int) -> list:
        """The game behind `pid` started over: out of every session, as if it had left each one,
        and the URLs it registered are no longer where it is. The invitations waiting for it
        still stand - the sessions they are for have not gone anywhere."""
        left = self.sessions_of(pid)
        for session in left:
            self.leave(session, pid)
        self.urls.pop(pid, None)
        return left

    def forget(self, pid: int) -> list:
        """A player whose game is gone: out of every session, as if it had left each one, with
        its URLs and the invitations waiting for it. Returns the sessions it was in."""
        left = self.restarted(pid)
        self.games.pop(pid, None)
        self.invitations = [i for i in self.invitations if i.recipient != pid]
        return left

    # ----- finding -----------------------------------------------------------------------------
    def search(self, caller: int, type_id: int, query_id: int, parameters, hosted=None) -> list:
        """The sessions a query matches, newest first, at most SEARCH_LIMIT.

        `hosted(session)` says whether its host is still there to be reached; a session waiting
        for migration, or whose host has gone quiet, is not offered to anybody.
        """
        params = dict(_pairs(parameters))
        found = []
        for session in sorted(self.by_id.values(), key=lambda s: -s.id):
            if matches(session, caller, type_id, query_id, params) and (
                    hosted is None or hosted(session)):
                found.append(session)
                if len(found) >= SEARCH_LIMIT:
                    break
        return found

    def with_participants(self, type_id: int, pids) -> list:
        wanted = set(pids or ())
        return [s for s in self.by_id.values()
                if s.type_id == type_id and wanted.intersection(s.participants())]

    # ----- invitations -------------------------------------------------------------------------
    def invite(self, session: Session, sender: int, recipient: int, message: str) -> Invitation:
        """One invitation, replacing any earlier one from the same sender to the same session -
        and the newest goes last, which is where the client looks for it."""
        self.invitations = [i for i in self.invitations
                            if not (i.recipient == recipient and i.sender == sender
                                    and i.session_id == session.id)]
        invitation = Invitation(recipient, sender, session.type_id, session.id, message or "",
                                pack_datetime())
        self.invitations.append(invitation)
        mine = [i for i in self.invitations if i.recipient == recipient]
        for old in mine[:-INVITATIONS_KEPT]:
            self.invitations.remove(old)
        return invitation

    def received(self, pid: int, type_id: int = None) -> list:
        return [i for i in self.invitations
                if i.recipient == pid and (type_id is None or i.type_id == type_id)]

    def sent(self, pid: int, type_id: int = None) -> list:
        return [i for i in self.invitations
                if i.sender == pid and (type_id is None or i.type_id == type_id)]

    def withdraw(self, recipient: int, sender: int, session_id: int) -> list:
        """Remove the invitation(s) from `sender` to `recipient` for this session."""
        gone = [i for i in self.invitations
                if i.recipient == recipient and i.session_id == session_id
                and (not sender or i.sender == sender)]
        for invitation in gone:
            self.invitations.remove(invitation)
        return gone

    # ----- the status page ---------------------------------------------------------------------
    def status(self, name_for=None) -> list:
        name_for = name_for or (lambda pid: "")
        return [{"type": s.type_id, "id": s.id, "host": s.owner, "host_name": name_for(s.owner),
                 "public": list(s.public), "private": list(s.private),
                 "capacity": list(s.capacity()), "used": list(s.used()),
                 "attributes": {"0x%X" % aid: value for aid, value in
                                sorted(s.attributes.items())},
                 "invitations": sum(1 for i in self.invitations if i.session_id == s.id)}
                for s in sorted(self.by_id.values(), key=lambda s: s.id)]


def matches(session: Session, caller: int, type_id: int, query_id: int, params: dict) -> bool:
    """Whether a query finds this session. The original server ran its queries as scripts that
    did not ship with the game, so this is the rule the client's own queries make sense under."""
    if session.type_id != type_id:
        return False
    if session.involves(caller):
        return False
    if session.attributes.get(INVITE_ONLY) == 1:
        return False
    free_public, _ = session.free()
    if free_public <= 0:
        return False
    for aid, value in params.items():
        if aid in NOT_EQUALITY:
            continue
        # A parameter the session does not carry does not rule it out: the client leaves a
        # parameter out to mean "any", and a host may not have written every one.
        if aid in session.attributes and session.attributes[aid] != value:
            return False
    wanted = params.get(PUBLIC_SLOTS)
    if query_id == QUERY_QUICK_MATCH and wanted is not None:
        # The host doubled the lobby size into its capacity; a half-full lobby must stay visible.
        return session.attributes.get(PUBLIC_SLOTS, 0) == 2 * wanted
    if wanted is not None:
        # The searcher's party has to fit.
        return free_public >= wanted
    return True


def _pairs(properties) -> list:
    """(id, value) from a Property list as decoded, skipping anything malformed."""
    out = []
    for p in properties or ():
        if isinstance(p, dict) and "m_ID" in p:
            out.append((p["m_ID"], p.get("m_value", 0)))
    return out
