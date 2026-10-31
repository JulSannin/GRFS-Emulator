"""Who is whose friend, and who has whom blocked.

Relationships are *directed edges*, not pairs, because a friend request has two sides at once:
the player who sent it sees "Pending" and the player who got it sees something to accept. Both
sides are stored, and the value on each side is what FriendsProtocol 13 GetRelationships sends,
which is what the friends screen sorts its tabs by:

    1  a friend                      FRIENDS, online or offline
    2  a request this player sent    FRIENDS too, shown as "Pending"
    3  a request waiting here        INVITATIONS

Those three were established by putting one account per value in front of the game and reading
the screen, and later confirmed in the client (TaskRDV_ListFriends 8CD050 turns 3 into 0x120 and
2 into 0x110). They are not in the game's schema - it carries no enumerations - so this is the
only record of them. An earlier guess had 3 meaning "friend", which put every friend under
INVITATIONS typed "Friend".

The BLOCKLIST tab is a list of its own, not a relationship value. It is filled from
FriendsProtocol 12 GetDetailedList asked about 3, which in that call means "blacklist": the
client marks every entry it gets back as blocked, whatever the entry says. That call used to
answer with the requests waiting here, so every request showed twice - under INVITATIONS and
under BLOCKLIST - while the players really blocked showed as ordinary offline friends. So
blocking is a flag on the blocker's edge, and the two lists are kept apart here:

* `contacts` - friends, requests sent, requests received; never anybody this player blocked,
  and never anybody who blocked this player.
* `blacklist` - only the players this player blocked.

The game has no "unblock" call. Its Unblock button sends a friend request by name, and its
Remove on the BLOCKLIST tab sends ClearRelationship, so both of those clear the block first.

Every change that alters what *another* player's lists would show is reported through
`on_change(their pid, actor pid, param2)`, which the friends service turns into a notification:
the client refetches its whole list a few seconds after one arrives, and it has no other way to
learn that somebody asked, answered or left. The player who acted is never told - its own
completion handlers mark its list for refetching.

Why this matters for co-op at all: Future Soldier finds a friend's game by asking which sessions
its contacts are in. With an empty friends list there is nothing to search for, and a hosted
co-op game can never be found.

Accounts are never deleted from here, only their relationship changed: removing a friend used to
delete the record, which threw away the pid and made that player unfindable, so removing somebody
could not be undone.
"""

import json
import os
import time

from . import log as logs

NOT_A_FRIEND = 0
FRIEND = 1
REQUEST_SENT = 2
REQUEST_RECEIVED = 3

NAMES = {NOT_A_FRIEND: "none", FRIEND: "friend", REQUEST_SENT: "request sent",
         REQUEST_RECEIVED: "request received"}

# What the previous implementation put in m_uiDetails, which the game keeps and never reads back.
DEFAULT_DETAILS = 1

# NotificationEvent m_uiParam2: the client posts "pending changed" for 2 and "friend changed" for
# 1, and both do the same thing - mark the list for refetching (9115A0, A4DD60).
NEW_REQUEST = 2
SOMETHING_CHANGED = 1

# How one player's lists show another, for telling whether a change is worth a notification.
BLOCKED = "blocked"


class Contact:
    """One entry of somebody's contact list. Presence is not here - see Backend.is_online."""

    __slots__ = ("name", "pid", "relationship", "details", "blocked", "message", "since")

    def __init__(self, name, pid, relationship, details=DEFAULT_DETAILS, blocked=False,
                 message="", since=0.0):
        self.name = name
        self.pid = pid
        self.relationship = relationship
        self.details = details
        self.blocked = blocked
        self.message = message
        self.since = since

    def __repr__(self):
        return "<%s 0x%X %s%s>" % (self.name, self.pid, NAMES.get(self.relationship),
                                   " blocked" if self.blocked else "")


class Edge:
    __slots__ = ("relationship", "details", "blocked", "message", "since")

    def __init__(self, relationship=NOT_A_FRIEND, details=DEFAULT_DETAILS, blocked=False,
                 message="", since=None):
        self.relationship = relationship
        self.details = details
        self.blocked = blocked
        self.message = message
        self.since = since if since is not None else time.time()


class Friends:
    def __init__(self, accounts, path: str = None, everyone: bool = False):
        self.accounts = accounts
        self.path = path
        # A private server for two people can skip the whole add-and-accept dance. Off by
        # default: the real flow works and is what the game expects, and inventing friendships
        # nobody asked for is confusing when something else is wrong. Blocking still works in
        # this mode, and the game's Unblock (a friend request) still undoes it.
        self.everyone = everyone
        # Called as on_change(pid told, pid who acted, param2) - see the module docstring.
        self.on_change = None
        self.log = logs.get("friends")
        self.edges = {}
        self.load()

    # ----- storage --------------------------------------------------------------------------
    def load(self) -> None:
        if not self.path or not os.path.exists(self.path):
            return
        try:
            with open(self.path, encoding="utf-8") as f:
                rows = json.load(f)
        except (OSError, ValueError) as e:
            self.log.error("cannot read %s (%s) - starting with nobody knowing anybody",
                           self.path, e)
            return
        for row in rows:
            self.edges[(row["from"].lower(), row["to"].lower())] = Edge(
                row.get("relationship", NOT_A_FRIEND), row.get("details", DEFAULT_DETAILS),
                row.get("blocked", False), row.get("message", ""), row.get("since"))
        self.log.info("%d relationship(s) from %s", len(self.edges), self.path)

    def save(self) -> None:
        if not self.path:
            return
        rows = [{"from": a, "to": b, "relationship": e.relationship, "details": e.details,
                 "blocked": e.blocked, "message": e.message, "since": e.since}
                for (a, b), e in sorted(self.edges.items())]
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(rows, f, indent=1)
            os.replace(tmp, self.path)
        except OSError as e:
            self.log.error("cannot write %s: %s", self.path, e)

    # ----- reading --------------------------------------------------------------------------
    def edge(self, a: str, b: str):
        return self.edges.get((a.lower(), b.lower()))

    def relationship(self, a: str, b: str) -> int:
        e = self.edge(a, b)
        return e.relationship if e else NOT_A_FRIEND

    def blocked(self, a: str, b: str) -> bool:
        """Whether `a` has `b` blocked."""
        e = self.edge(a, b)
        return bool(e and e.blocked)

    def contacts(self, name: str) -> list:
        """Everyone in `name`'s GetRelationships: friends, requests either way. Nobody `name`
        blocked - they are in `blacklist` - and nobody who blocked `name`."""
        if not name:
            return []
        lower = name.lower()
        out = []
        if self.everyone:
            for account in self.accounts.by_name.values():
                if account.name.lower() == lower or self._blocked_either_way(name, account.name):
                    continue
                out.append(Contact(account.name, account.pid, FRIEND))
            return sorted(out, key=lambda c: c.pid)
        for (a, b), e in self.edges.items():
            if a != lower or e.relationship == NOT_A_FRIEND:
                continue
            account = self.accounts.get(b)
            if account is None or self._blocked_either_way(name, account.name):
                continue
            out.append(Contact(account.name, account.pid, e.relationship, e.details, False,
                               e.message, e.since))
        return sorted(out, key=lambda c: c.pid)

    def friends_of(self, name: str) -> list:
        """The confirmed friends only."""
        return [c for c in self.contacts(name) if c.relationship == FRIEND]

    def blacklist(self, name: str) -> list:
        """The players `name` has blocked: the BLOCKLIST tab."""
        if not name:
            return []
        lower = name.lower()
        out = []
        for (a, b), e in self.edges.items():
            if a != lower or not e.blocked:
                continue
            account = self.accounts.get(b)
            if account is None:
                continue
            out.append(Contact(account.name, account.pid, NOT_A_FRIEND, e.details, True,
                               "", e.since))
        return sorted(out, key=lambda c: c.pid)

    def messages(self, name: str) -> list:
        """(sender pid, text) of the requests waiting for `name` to answer."""
        return [(c.pid, c.message) for c in self.contacts(name)
                if c.relationship == REQUEST_RECEIVED]

    def view(self, a: str, b: str):
        """What `a`'s lists show about `b`: None, BLOCKED, or (relationship, request text).

        Two moments' views differing is exactly "b did something a needs to refetch for"."""
        if not self._both_known(a, b, quiet=True):
            return None
        if self.blocked(a, b):
            return BLOCKED
        if self.blocked(b, a):
            return None
        if self.everyone:
            return (FRIEND, "")
        e = self.edge(a, b)
        if e is None or e.relationship == NOT_A_FRIEND:
            return None
        return (e.relationship, e.message if e.relationship == REQUEST_RECEIVED else "")

    # ----- changing -------------------------------------------------------------------------
    def _set(self, a: str, b: str, relationship: int, details: int = None,
             message: str = None) -> Edge:
        key = (a.lower(), b.lower())
        e = self.edges.get(key)
        if e is None:
            e = Edge(relationship)
            self.edges[key] = e
        e.relationship = relationship
        if details is not None:
            e.details = details
        if message is not None:
            e.message = message
        return e

    def _clear_block(self, a: str, b: str) -> bool:
        e = self.edge(a, b)
        if e is None or not e.blocked:
            return False
        e.blocked = False
        self.log.info("%s unblocked %s", a, b)
        return True

    def request(self, a: str, b: str, message: str = "",
                details: int = DEFAULT_DETAILS) -> bool:
        """`a` asks `b` to be friends. Asking somebody whose request is already waiting means yes.

        This is also how the game unblocks: its Unblock button sends a request by name and
        nothing else, so a block `a` had on `b` is lifted first. A block `b` has on `a` stays,
        and the request is refused.

        Adding used to make both sides friends outright, which skipped the half the other player
        is supposed to see and decide about.
        """
        if not self._both_known(a, b):
            return False
        before = self.view(b, a)
        unblocked = self._clear_block(a, b)
        if self.blocked(b, a):
            self.log.info("%s asked %s, who has them blocked", a, b)
            if unblocked:
                self.save()
            return False
        if not self.everyone:
            relationship = self.relationship(a, b)
            if relationship == REQUEST_RECEIVED:
                if unblocked:
                    self.save()
                return self.accept(a, b)
            if relationship != FRIEND:
                self._set(a, b, REQUEST_SENT, details)
                # Asking again keeps the text that came the first time unless there is new
                # text: the game sends the request with none and the text a moment later.
                self._set(b, a, REQUEST_RECEIVED, details,
                          message if (message or relationship != REQUEST_SENT) else None)
                self.log.info("%s asked %s to be friends%s", a, b,
                              (" (%s)" % message) if message else "")
        self.save()
        self._changed(a, b, before)
        return True

    def accept(self, a: str, b: str) -> bool:
        """`a` accepts the request `b` sent."""
        if self.everyone:
            return self.view(a, b) == (FRIEND, "")
        if self.relationship(a, b) != REQUEST_RECEIVED or self.blocked(b, a):
            self.log.info("%s accepted %s, who never asked (relationship is %s)", a, b,
                          NAMES.get(self.relationship(a, b)))
            return False
        before = self.view(b, a)
        # The text came with the request; once it is answered there is no invitation to read.
        self._set(a, b, FRIEND, message="")
        self._set(b, a, FRIEND, message="")
        self.log.info("%s and %s are friends", a, b)
        self.save()
        self._changed(a, b, before)
        return True

    def decline(self, a: str, b: str) -> bool:
        """`a` says no to the request `b` sent. Only a request can be declined - removing a
        friend is `clear`."""
        if self.everyone or self.relationship(a, b) != REQUEST_RECEIVED:
            return False
        return self.clear(a, b)

    def clear(self, a: str, b: str) -> bool:
        """Remove a friend, decline a request, cancel one - all of them end the relationship.
        On the BLOCKLIST tab the same call is "Remove", so it lifts a block too.

        The accounts stay registered, keep their pids, and remain findable, so this can be undone.
        """
        before_rel = self.relationship(a, b)
        unblocked = self.blocked(a, b)
        if (before_rel == NOT_A_FRIEND or self.everyone) and not unblocked:
            return False
        before = self.view(b, a)
        self._clear_block(a, b)
        if not self.everyone:
            self._set(a, b, NOT_A_FRIEND, message="")
            self._set(b, a, NOT_A_FRIEND, message="")
        if before_rel != NOT_A_FRIEND:
            self.log.info("%s ended being %s with %s", a, NAMES.get(before_rel), b)
        self.save()
        self._changed(a, b, before)
        return True

    def block(self, a: str, b: str) -> bool:
        """`a` blocks `b`. Whatever was between them ends: `b` loses `a` from every list, and
        cannot ask again until `a` unblocks them."""
        if not self._both_known(a, b):
            return False
        before = self.view(b, a)
        self._set(a, b, NOT_A_FRIEND, message="").blocked = True
        self._set(b, a, NOT_A_FRIEND, message="")
        self.log.info("%s blocked %s", a, b)
        self.save()
        self._changed(a, b, before)
        return True

    def unblock(self, a: str, b: str) -> bool:
        """Only the Uplay side (UbiFriends RemoveFromBlacklist) unblocks without asking."""
        if not self._both_known(a, b, quiet=True):
            return False
        before = self.view(b, a)
        if not self._clear_block(a, b):
            return False
        self.save()
        self._changed(a, b, before)
        return True

    def set_details(self, a: str, b: str, details: int) -> bool:
        e = self.edge(a, b)
        if e is None:
            return False
        e.details = details
        self.save()
        return True

    def drop_message(self, name: str, sender_pid: int) -> bool:
        """Dismiss the text of a waiting request without answering the request itself."""
        sender = self.accounts.name_for(sender_pid)
        e = self.edge(name, sender) if sender else None
        if e is None or not e.message:
            return False
        e.message = ""
        self.save()
        return True

    def search(self, part: str, excluding: str = None) -> list:
        """Accounts whose name contains `part`. FIND FRIENDS searches the register, so a player
        has to have signed in here at least once to be findable."""
        part = (part or "").lower()
        out = []
        for account in self.accounts.by_name.values():
            if excluding and account.name.lower() == excluding.lower():
                continue
            if part in account.name.lower():
                out.append(account)
        return sorted(out, key=lambda a: a.pid)

    # -----------------------------------------------------------------------------------------
    def _blocked_either_way(self, a: str, b: str) -> bool:
        return self.blocked(a, b) or self.blocked(b, a)

    def _both_known(self, a: str, b: str, quiet: bool = False) -> bool:
        """Only accounts that exist get relationships, so a typo cannot leave an edge behind
        pointing at nobody."""
        if not a or not b or a.lower() == b.lower():
            return False
        if self.accounts.get(a) is None or self.accounts.get(b) is None:
            if not quiet:
                self.log.info("%s and %s: one of them has never signed in here", a, b)
            return False
        return True

    def _changed(self, actor: str, other: str, before) -> None:
        """Tell `other` if what their lists show about `actor` is no longer `before`."""
        after = self.view(other, actor)
        if after == before or self.on_change is None:
            return
        fresh = (isinstance(after, tuple) and after[0] == REQUEST_RECEIVED
                 and not (isinstance(before, tuple) and before[0] == REQUEST_RECEIVED))
        to, by = self.accounts.get(other), self.accounts.get(actor)
        if to is None or by is None:
            return
        try:
            self.on_change(to.pid, by.pid, NEW_REQUEST if fresh else SOMETHING_CHANGED)
        except Exception:
            # A notification that could not go out must not undo the change it was about.
            self.log.exception("telling %s about %s", other, actor)
