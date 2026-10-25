"""Who is whose friend.

Relationships are *directed edges*, not pairs, because a friend request has two sides at once:
the player who sent it sees "Pending" and the player who got it sees something to accept. Both
sides are stored, and the value on each side is what the friends screen sorts its tabs by:

    1  a friend                 UPLAY FRIENDS, with Invite / Block / Remove in its menu
    2  a request this player sent    the same list, shown as "Pending"
    3  a request waiting here        leaves that list and appears under INVITATIONS

Those three were established by putting one account per value in front of the game and reading
the screen. They are not in the game's schema - it carries no enumerations - so this is the only
record of them. An earlier guess had 3 meaning "friend", which put every friend under
INVITATIONS typed "Friend".

Why this matters for co-op at all: Future Soldier finds a friend's game by asking which sessions
its contacts are in. With an empty friends list there is nothing to search for, and a hosted
co-op game can never be found.

Blocking is kept as a flag of its own rather than a relationship value, which is how the game's
own structures treat it (RelationshipInfo has both a relationship and an m_onBlacklist).

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
        # nobody asked for is confusing when something else is wrong.
        self.everyone = everyone
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

    def contacts(self, name: str) -> list:
        """Everyone `name` has a relationship with, with the name and pid filled in."""
        if not name:
            return []
        out = []
        if self.everyone:
            for account in self.accounts.by_name.values():
                if account.name.lower() == name.lower():
                    continue
                e = self.edge(name, account.name)
                if e is not None and e.blocked:
                    continue
                out.append(Contact(account.name, account.pid, FRIEND))
            return sorted(out, key=lambda c: c.pid)
        lower = name.lower()
        for (a, b), e in self.edges.items():
            if a != lower or (e.relationship == NOT_A_FRIEND and not e.blocked):
                continue
            account = self.accounts.get(b)
            if account is None:
                continue
            out.append(Contact(account.name, account.pid, e.relationship, e.details, e.blocked,
                               e.message, e.since))
        return sorted(out, key=lambda c: c.pid)

    def messages(self, name: str) -> list:
        """(sender pid, text) of the requests waiting for `name` to answer."""
        return [(c.pid, c.message) for c in self.contacts(name)
                if c.relationship == REQUEST_RECEIVED]

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

    def request(self, a: str, b: str, message: str = "",
                details: int = DEFAULT_DETAILS) -> bool:
        """`a` asks `b` to be friends. Asking somebody whose request is already waiting means yes.

        Adding used to make both sides friends outright, which skipped the half the other player
        is supposed to see and decide about.
        """
        if not self._both_known(a, b):
            return False
        if self.relationship(a, b) == REQUEST_RECEIVED:
            return self.accept(a, b)
        if self.relationship(a, b) == FRIEND:
            return True
        if self.blocked(b, a):
            self.log.info("%s asked %s, who has them blocked", a, b)
            return False
        self._set(a, b, REQUEST_SENT, details)
        self._set(b, a, REQUEST_RECEIVED, details, message)
        self.log.info("%s asked %s to be friends%s", a, b,
                      (" (%s)" % message) if message else "")
        self.save()
        return True

    def accept(self, a: str, b: str) -> bool:
        """`a` accepts the request `b` sent."""
        if self.relationship(a, b) != REQUEST_RECEIVED:
            self.log.info("%s accepted %s, who never asked (relationship is %s)", a, b,
                          NAMES.get(self.relationship(a, b)))
            return False
        # The text came with the request; once it is answered there is no invitation to read.
        self._set(a, b, FRIEND, message="")
        self._set(b, a, FRIEND, message="")
        self.log.info("%s and %s are friends", a, b)
        self.save()
        return True

    def decline(self, a: str, b: str) -> bool:
        return self.clear(a, b)

    def clear(self, a: str, b: str) -> bool:
        """Remove a friend, decline a request, cancel one - all of them end the relationship.

        The accounts stay registered, keep their pids, and remain findable, so this can be undone.
        """
        before = self.relationship(a, b)
        if before == NOT_A_FRIEND and not self.blocked(a, b):
            return False
        self._set(a, b, NOT_A_FRIEND, message="")
        self._set(b, a, NOT_A_FRIEND, message="")
        self.log.info("%s ended being %s with %s", a, NAMES.get(before), b)
        self.save()
        return True

    def blocked(self, a: str, b: str) -> bool:
        e = self.edge(a, b)
        return bool(e and e.blocked)

    def _both_known(self, a: str, b: str) -> bool:
        """Only accounts that exist get relationships, so a typo cannot leave an edge behind
        pointing at nobody."""
        if not a or not b or a.lower() == b.lower():
            return False
        if self.accounts.get(a) is None or self.accounts.get(b) is None:
            self.log.info("%s and %s: one of them has never signed in here", a, b)
            return False
        return True

    def block(self, a: str, b: str) -> bool:
        if not self._both_known(a, b):
            return False
        self._set(a, b, NOT_A_FRIEND, message="").blocked = True
        self._set(b, a, NOT_A_FRIEND, message="")
        self.log.info("%s blocked %s", a, b)
        self.save()
        return True

    def unblock(self, a: str, b: str) -> bool:
        e = self.edge(a, b)
        if e is None or not e.blocked:
            return False
        e.blocked = False
        self.log.info("%s unblocked %s", a, b)
        self.save()
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
