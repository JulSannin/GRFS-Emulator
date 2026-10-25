"""FriendsProtocol (0x14): the friends screen, all thirteen methods the schema declares.

    list         11 GetList   12 GetDetailedList   13 GetRelationships
    FIND         2 AddFriendByName        (SearchFriendsProtocol does the searching)
    menu         9 ClearRelationship      "Remove Friend" and "Remove"
                 7 BlackList              "Block"
    INVITATIONS  5 AcceptFriendship       6 DeclineFriendship

Three things here were each paid for with a run of the game:

* 13 GetRelationships returns a *total count* before the list. Without it the client took the
  list length for the total, then read the list's own length out of the first entry's pid, and
  the contact list stayed empty whatever was in the entries.
* byRelationship on 11 and 12 is a *filter*, not a label to echo back. Answering every account
  with the asked-for value stamped on it put every friend under INVITATIONS, because the client
  only ever sends those calls asking about 3 - requests waiting to be answered.
* The status byte is 0 for Offline and anything else for Online. An offline friend stays in the
  list; the client just stops looking for their game.

Presence needs no protocol of its own: the game sends a packet about once a second, each one
stamps when it was last heard from, and silence is the only honest signal there is.
"""

from .. import friends as model
from .. import protocols
from ..rmc.router import Errors, RmcError, Service


class Friends(Service):
    protocol_id = protocols.FRIENDS
    protocol_name = "FriendsProtocol"

    # ----- the list --------------------------------------------------------------------------
    def GetList(self, ctx, byRelationship, bReversed=False):
        """Just the pids. `bReversed` is accepted and ignored: both directions of every
        relationship are stored, so the stored value already answers either question."""
        return {"lstFriendsList": [c.pid for c in self._filtered(ctx, byRelationship)]}

    def GetDetailedList(self, ctx, byRelationship, bReversed=False):
        """The entries the friends screen draws. Status here is free text - the game's own words,
        unlike the byte GetRelationships uses."""
        return {"lstFriendsList": [
            {"m_pid": c.pid, "m_strName": c.name, "m_byRelationship": c.relationship,
             "m_uiDetails": c.details,
             "m_strStatus": "Online" if self.backend.is_online(c.pid) else "Offline"}
            for c in self._filtered(ctx, byRelationship)]}

    def GetRelationships(self, ctx, resultRange=None):
        contacts = self._contacts(ctx)
        offset, size = 0, len(contacts)
        if isinstance(resultRange, dict):
            offset = resultRange.get("m_uiOffset", 0) or 0
            size = resultRange.get("m_uiSize", 0) or len(contacts)
        page = contacts[offset:offset + size]
        self.log.info("%s has %d contact(s)%s: %s", ctx.client.name, len(contacts),
                      "" if len(page) == len(contacts) else " (%d shown)" % len(page),
                      ", ".join("%s %s%s" % (c.name, model.NAMES.get(c.relationship),
                                             "" if self.backend.is_online(c.pid) else " offline")
                                for c in page) or "-")
        return {
            # The total across every page, which is not the same as this page's length. They are
            # equal here - the client asks for a range wide enough for everyone.
            "uiTotalCount": len(contacts),
            "lstRelationshipsList": [
                {"m_pid": c.pid, "m_strName": c.name, "m_byRelationship": c.relationship,
                 "m_uiDetails": c.details,
                 "m_byStatus": 1 if self.backend.is_online(c.pid) else 0}
                for c in page],
        }

    # ----- adding ----------------------------------------------------------------------------
    def AddFriend(self, ctx, uiPlayer, uiDetails=model.DEFAULT_DETAILS, strMessage=""):
        return {"retval": self._ask(ctx, self._name_of(uiPlayer), strMessage, uiDetails)}

    def AddFriendByName(self, ctx, strPlayerName, uiDetails=model.DEFAULT_DETAILS,
                        strMessage=""):
        return {"retval": self._ask(ctx, strPlayerName, strMessage, uiDetails)}

    def AddFriendWithDetails(self, ctx, uiPlayer, uiDetails=model.DEFAULT_DETAILS,
                             strMessage=""):
        name = self._name_of(uiPlayer)
        self._ask(ctx, name, strMessage, uiDetails)
        return {"relationshipData": self._entry(ctx, name)}

    def AddFriendByNameWithDetails(self, ctx, strPlayerName, uiDetails=model.DEFAULT_DETAILS,
                                   strMessage=""):
        self._ask(ctx, strPlayerName, strMessage, uiDetails)
        return {"relationshipData": self._entry(ctx, strPlayerName)}

    # ----- answering and ending --------------------------------------------------------------
    def AcceptFriendship(self, ctx, uiPlayer):
        return {"retval": self.backend.friends.accept(self._me(ctx), self._name_of(uiPlayer))}

    def DeclineFriendship(self, ctx, uiPlayer):
        return {"retval": self.backend.friends.decline(self._me(ctx), self._name_of(uiPlayer))}

    def ClearRelationship(self, ctx, uiPlayer):
        return {"retval": self.backend.friends.clear(self._me(ctx), self._name_of(uiPlayer))}

    def BlackList(self, ctx, uiPlayer, uiDetails=0):
        return {"retval": self.backend.friends.block(self._me(ctx), self._name_of(uiPlayer))}

    def BlackListByName(self, ctx, strPlayerName, uiDetails=0):
        return {"retval": self.backend.friends.block(self._me(ctx), strPlayerName)}

    def UpdateDetails(self, ctx, uiPlayer, uiDetails):
        """uiDetails is a number the game keeps about a friend; nothing reads it back, so
        remembering it is the whole job."""
        self.backend.friends.set_details(self._me(ctx), self._name_of(uiPlayer), uiDetails)
        return {"retval": True}

    # -----------------------------------------------------------------------------------------
    def _me(self, ctx) -> str:
        if not ctx.client.name:
            raise RmcError(Errors.RV_NOT_AUTHENTICATED, "the friends screen before logging in")
        return ctx.client.name

    def _name_of(self, pid: int) -> str:
        name = self.backend.accounts.name_for(pid)
        if not name:
            self.log.info("no account here has pid 0x%X", pid)
        return name

    def _contacts(self, ctx) -> list:
        return self.backend.friends.contacts(self._me(ctx))

    def _filtered(self, ctx, relationship) -> list:
        contacts = self._contacts(ctx)
        if not relationship:
            return contacts
        return [c for c in contacts if c.relationship == relationship]

    def _ask(self, ctx, name: str, message: str, details: int) -> bool:
        """Adding somebody who is not here answers false rather than failing the call: these
        methods declare a bool for exactly this, and the screen handles it."""
        if not name or self.backend.accounts.get(name) is None:
            self.log.info("%s tried to add %s, who has never signed in here", ctx.client.name,
                          name or "(nobody)")
            return False
        return self.backend.friends.request(self._me(ctx), name, message, details)

    def _entry(self, ctx, name: str) -> dict:
        """One RelationshipData for a player, for the two calls that return it."""
        account = self.backend.accounts.get(name)
        pid = account.pid if account else 0
        return {"m_pid": pid, "m_strName": account.name if account else (name or ""),
                "m_byRelationship": self.backend.friends.relationship(self._me(ctx), name),
                "m_uiDetails": model.DEFAULT_DETAILS,
                "m_byStatus": 1 if pid and self.backend.is_online(pid) else 0}
