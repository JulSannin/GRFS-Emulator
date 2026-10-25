"""FriendMessagesExtensionServiceProtocol (0x57): the text that comes with a friend request.

This is friend requests, not squad invitations - those are GameSessionProtocol. The confusion
cost the previous attempt an afternoon of looking in the wrong protocol for why an invitation
never arrived.
"""

from .. import protocols
from ..rmc.router import Errors, RmcError, Service


class FriendMessages(Service):
    protocol_id = protocols.FRIEND_MESSAGES_EXT
    protocol_name = "FriendMessagesExtensionServiceProtocol"

    def SendFriendInvitationMessage(self, ctx, receiptPid, messageBody):
        me = self._me(ctx)
        other = self.backend.accounts.name_for(receiptPid)
        if not other:
            raise RmcError(Errors.FRIENDS_USER_NOT_FOUND, "no account here has pid 0x%X" % receiptPid)
        self.backend.friends.request(me, other, messageBody)
        self.log.info("%s asked %s to be friends: %s", me, other, messageBody or "(no text)")
        return None

    def ReceiveFriendInvitationMessages(self, ctx):
        messages = self.backend.friends.messages(self._me(ctx))
        return {"messages": [{"m_senderPid": pid, "m_message": text}
                             for pid, text in messages]}

    def RemoveFriendInvitationMessage(self, ctx, senderPid):
        """Dismisses the text, not the request - answering a request is Accept or Decline on
        FriendsProtocol."""
        self.backend.friends.drop_message(self._me(ctx), senderPid)
        return None

    def _me(self, ctx) -> str:
        if not ctx.client.name:
            raise RmcError(Errors.RV_NOT_AUTHENTICATED, "friend messages before logging in")
        return ctx.client.name
