"""UbiFriendsProtocol (0x2F): Ubisoft's own friends layer, beside Quazal's.

Every refetch of the friends screen calls 7 GetRelationshipsList straight after FriendsProtocol
13 GetRelationships, inside the same composite, so failing it fails the whole fetch. What it
returns is merged into the same list: each entry becomes a contact with state 0 - "offline" -
unless a contact of that name is already there (TaskUbi_RetrieveFriendList 913B40). Since 13
has already listed everyone, this answers with the same set and adds nothing new; and it never
includes a blocked player, because one would show as an offline friend beside its BLOCKLIST
entry. m_relationship and m_onBlacklist are only logged by the client.

An entry here carries no name, only an account id, which is why the client goes straight on to
UbiAccountManagement 14 to turn those ids into names (see ubi_accounts).

The actions are the same as FriendsProtocol's and go through the same store, so they notify
the other player in the same way. RemoveFromBlacklist is the only unblock that is not a friend
request; the game itself never sends it.
"""

from .. import protocols
from ..rmc.router import Errors, RmcError, Service
from ..rmc.stream import pack_datetime


class UbiFriends(Service):
    protocol_id = protocols.UBI_FRIENDS
    protocol_name = "UbiFriendsProtocol"

    def GetRelationshipsList(self, ctx):
        me = self._me(ctx)
        mine = {"m_principalId": ctx.client.pid,
                "m_ubiAccountId": self.backend.ubi_account_id(me)}
        rows = []
        for contact in self.backend.friends.contacts(me):
            rows.append({
                "m_from": mine,
                "m_to": {"m_principalId": contact.pid,
                         "m_ubiAccountId": self.backend.ubi_account_id(contact.name)},
                "m_relationship": contact.relationship,
                "m_onBlacklist": False,
                # Quazal packs a date into one 64-bit field rather than counting from an epoch.
                "m_friendsDate": pack_datetime(),
            })
        self.log.debug("%s: %d relationship(s) for the Uplay tab", me, len(rows))
        return {"relationshipsList": rows}

    # The same five actions as FriendsProtocol, reached from the Uplay side of the screen. They
    # name the other player by account id, which here is the account name.
    def RequestFriendship(self, ctx, to):
        return self._act(ctx, to, self.backend.friends.request)

    def AcceptFriendship(self, ctx, to):
        return self._act(ctx, to, self.backend.friends.accept)

    def DeclineFriendship(self, ctx, to):
        return self._act(ctx, to, self.backend.friends.decline)

    def ClearRelationship(self, ctx, to):
        return self._act(ctx, to, self.backend.friends.clear)

    def AddToBlacklist(self, ctx, to):
        return self._act(ctx, to, self.backend.friends.block)

    def RemoveFromBlacklist(self, ctx, to):
        return self._act(ctx, to, self.backend.friends.unblock)

    # -----------------------------------------------------------------------------------------
    def _me(self, ctx) -> str:
        if not ctx.client.name:
            raise RmcError(Errors.RV_NOT_AUTHENTICATED, "the friends screen before logging in")
        return ctx.client.name

    def _act(self, ctx, to, action):
        """These methods declare no return value, so the answer is an empty success either way;
        what happened goes to the log."""
        me = self._me(ctx)
        other = self._name_of(to)
        if not other:
            raise RmcError(Errors.FRIENDS_USER_NOT_FOUND, "no such player on this server")
        done = action(me, other)
        self.log.info("%s -> %s: %s%s", me, other, action.__name__,
                      "" if done else " (nothing changed)")
        return None

    def _name_of(self, account_info) -> str:
        """An AccountInfo names a player twice over - by pid and by account id. Either will do."""
        if not isinstance(account_info, dict):
            return ""
        pid = account_info.get("m_principalId") or 0
        if pid:
            name = self.backend.accounts.name_for(pid)
            if name:
                return name
        return self.backend.name_for_ubi_account_id(account_info.get("m_ubiAccountId") or "")
