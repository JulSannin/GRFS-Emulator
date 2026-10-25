"""The RMC services.

Each module here answers one protocol. They are listed below rather than discovered, so that
what the server answers is a visible list in one place.
"""


def all_services() -> list:
    from .auth import TicketGranting
    from .friend_messages import FriendMessages
    from .friends import Friends
    from .privileges import Privileges
    from .search_friends import SearchFriends
    from .secure import SecureConnection
    from .ubi_accounts import UbiAccounts
    from .ubi_friends import UbiFriends
    return [
        # Signing in
        TicketGranting,         # 0x0A
        SecureConnection,       # 0x0B
        Privileges,             # 0x23
        # The friends screen, which is how one player reaches another
        Friends,                # 0x14
        UbiFriends,             # 0x2F
        UbiAccounts,            # 0x1D
        SearchFriends,          # 0x56
        FriendMessages,         # 0x57
    ]
