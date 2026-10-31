"""SearchFriendsProtocol (0x56): FIND FRIENDS.

Searches this server's own account register, so a player has to have signed in here at least
once before anyone can find them. The player doing the searching is left out of their own
results.
"""

from .. import protocols
from ..rmc.router import Service


class SearchFriends(Service):
    protocol_id = protocols.SEARCH_FRIENDS
    protocol_name = "SearchFriendsProtocol"

    def SearchFriends(self, ctx, partOfName):
        me = ctx.client.name
        found = self.backend.friends.search(partOfName, excluding=me)
        self.log.info('%s searched for "%s": %s', me or ctx.client.address, partOfName,
                      ", ".join(a.name for a in found) or "nobody")
        return {"foundList": [{"m_pid": a.pid, "m_strName": a.name} for a in found]}
