"""The RMC services.

Each module here answers one protocol, or a few small ones that belong together. They are listed
below rather than discovered, so that what the server answers is a visible list in one place.

Not answered, on purpose
------------------------
Everything else the game's schema has is in NOT_ANSWERED below, each with the reason, and a
test (tests.test_live_sweep) holds the two lists together: a method is either answered or in
there. A call to anything in it gets the router's error, never silence. The reasons come from the
client's code: a method is left out only when nothing in the game can send it, or - for
AccountManagement - when what sends it follows an answer this server never gives.
"""

# (protocol name, method ids or None for every method, why). Ids the schema does not declare
# are listed too, so that a call to one in a log can be looked up here.
NOT_ANSWERED = [
    ("AccountManagementProtocol", None,
     "the client turns to it (CreateAccount, CustomCreateAccount) only after a login answered"
     " 0x80030064, which this server sends only to a login with no name - the game always sends"
     " one; its other senders are JobManageAccount steps nothing in the game starts"),
    ("TicketGrantingProtocol", (4, 5, 6),
     "GetPID, GetName, LoginWithContext: no stub, builder or inlined header in the client"),
    ("UbiAccountManagementProtocol", (1, 3, 5, 6, 7, 8),
     "behind HermesWrapper slots 109, 110 and 112, which nothing dispatches, or under roots"
     " with no reference (CreateAccount, GetAccount, GetTOS, the Validate* steps)"),
    ("UbiAccountManagementProtocol", (2, 4, 9, 10, 18, 19, 20, 21, 22),
     "no stub in the client at all"),
    ("MiscProtocol", (2,),
     "SendGenericEvent: its only task creator, 8B0080, has no reference"),
    ("ClansProtocol", (4, 10, 25, 29, 30, 31, 32, 33),
     "DeleteClan, GetClanInvites, SendClanMatch/SendClanMessage and 29..33: their service"
     " functions or task constructors have no reference, or there is no caller at all"),
    ("UplayWinProtocol", (2, 3, 4, 6, 10, 11, 12),
     "the reply dispatcher can decode them, but the client has no request stub that sends them"),
    ("OverlordAwardsProtocol", (1, 2, 3, 5, 6, 7),
     "no stub in the client (only seven Overlord send sites exist)"),
    ("OverlordRpneProtocol", (2, 3, 4, 5), "no stub in the client"),
    ("OverlordDareProtocol", (3, 4, 5), "no stub in the client"),
    ("OverlordNewsProtocol", (2,), "no stub in the client, only a reply decoder"),
    ("NotificationProtocol", None,
     "the server calls the client with it (backend.notify), never the other way"),
    ("PersistentStoreProtocol", None,
     "registered, but every front-end that sends it is unreferenced (InsertFile, RemoveFile,"
     " GetFile) and the other methods have no stub"),
    ("NewsProtocol", None, "registered, but no method has a stub; Overlord's GetNews is used"),
    ("UbiNewsProtocol", None, "registered, but no method has a stub"),
    ("MatchMakingProtocol", None, "never registered: no protocol vtable, no SetID; co-op is"
                                  " GameSession"),
    ("MatchMakingProtocolExt", None, "never registered"),
    ("MessageDeliveryProtocol", None, "never registered"),
    ("GameSessionAdminProtocol", None, "never registered"),
    ("HealthProtocol", None, "server-side in the DDL; no client class"),
    ("MonitoringProtocol", None, "server-side in the DDL; no client class"),
    ("RemoteLogDeviceProtocol", None, "server-side in the DDL; no client class"),
    ("LocalizationAdminProtocol", None, "server-side in the DDL; no client class"),
    ("NewsProtocolAdmin", None, "server-side in the DDL; no client class"),
    ("TitleStorageAdminProtocol", None, "server-side in the DDL; no client class"),
    ("MetaSessionProtocol", None, "0x70: all 16 wrappers that call its stubs are unreferenced"),
    ("GameInfoProtocol", None, "0x71: every service function that sends it is unreferenced"),
    ("ContactsExtensionsProtocol", None, "0x72: its stubs have no reference"),
    ("HermesAchievementsProtocol", None,
     "0x74: its wrappers have no reference; the achievement service only sets an error"),
    ("SocialNetworksProtocol", None, "0x75: its stubs have no reference"),
    ("GameStorageFileVersionProtocol", None, "0x76: its only task is built by an unreferenced"
                                             " function"),
    ("IP2LocationProtocol", None, "0x7C: its stub has no reference"),
    ("OnlinePassProtocol", None,
     "0x7F: FreeTrial is a vtable slot nothing dispatches; the online pass itself is switched"
     " off by PassportKillSwitch in the configuration (grfs.onlineconfig)"),
    ("OverlordExtraContentProtocol", None, "never registered; its code is stripped"),
    ("OverlordChallengeProtocol", None, "never registered; its code is stripped"),
]


def not_answered(protocol: str, method: int = None):
    """Why this protocol, or this method of it, is not answered - or None if it should be."""
    for name, methods, why in NOT_ANSWERED:
        if name == protocol and (methods is None or method in methods):
            return why
    return None


def all_services() -> list:
    from .auth import TicketGranting
    from .clans import Clans
    from .friend_messages import FriendMessages
    from .friends import Friends
    from .game_session import GameSession
    from .localization import Localization
    from .map_list import MapList
    from .misc import Misc
    from .nat_traversal import NatTraversal
    from .overlord import (OverlordAwards, OverlordCore, OverlordDare, OverlordFriends,
                           OverlordNews, OverlordRpne)
    from .player_statistics import PlayerStatistics
    from .privileges import Privileges
    from .rich_presence import RichPresence
    from .search_friends import SearchFriends
    from .secure import SecureConnection
    from .title_storage import TitleStorage
    from .tracking import EventsTracking, Tracking, TrackingExtension
    from .ubi_accounts import UbiAccounts
    from .ubi_friends import UbiFriends
    from .uplay import GR5Award, UplayWin
    return [
        # Signing in
        TicketGranting,         # 0x0A
        SecureConnection,       # 0x0B
        Privileges,             # 0x23
        Localization,           # 0x27
        TitleStorage,           # 0x33
        # The friends screen, which is how one player reaches another
        Friends,                # 0x14
        UbiFriends,             # 0x2F
        UbiAccounts,            # 0x1D
        SearchFriends,          # 0x56
        FriendMessages,         # 0x57
        RichPresence,           # 0x6D
        # Playing together
        GameSession,            # 0x2A
        NatTraversal,           # 0x03
        MapList,                # 0x79
        Misc,                   # 0x6F
        # Progress, clans, rewards
        PlayerStatistics,       # 0x6C
        Clans,                  # 0x6E
        GR5Award,               # 0x77
        UplayWin,               # 0x78
        # Telemetry
        Tracking,               # 0x24
        TrackingExtension,      # 0x7D
        EventsTracking,         # 0x7E
        # Overlord, Ubisoft's post-login layer
        OverlordRpne,           # 0x1389
        OverlordNews,           # 0x138A
        OverlordCore,           # 0x138B
        OverlordFriends,        # 0x138D
        OverlordAwards,         # 0x138E
        OverlordDare,           # 0x1390
    ]
