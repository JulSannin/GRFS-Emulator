"""Protocol ids.

The DDL in the game's executable declares every protocol's methods but not its id, so the two
have to be joined by hand. Each id below is either confirmed in captured traffic (the client
called it and the reply was accepted) or taken from the documentation of other Quazal titles of
the same years and marked as such.

The ids are a flat numbering shared by the SDK's own protocols (low numbers), Ubisoft's Hermes
layer (0x6C and up) and Overlord (0x1389 and up).
"""

# --- OSDK, confirmed in GRFS traffic ------------------------------------------------------
NAT_TRAVERSAL = 0x03          # and the relay half, which is server-initiated
TICKET_GRANTING = 0x0A        # "Authentication" in the logs
SECURE_CONNECTION = 0x0B      # "Secure"
NOTIFICATION = 0x0E           # server -> client only
FRIENDS = 0x14
MATCH_MAKING = 0x15
PERSISTENT_STORE = 0x18
ACCOUNT_MANAGEMENT = 0x19
MESSAGE_DELIVERY = 0x1B
UBI_ACCOUNT_MANAGEMENT = 0x1D
NEWS = 0x1F
UBI_NEWS = 0x21
PRIVILEGES = 0x23
TRACKING = 0x24               # "Telemetry" in the logs
LOCALIZATION = 0x27
GAME_SESSION = 0x2A           # co-op and the squad live here, not in MatchMaking
GAME_SESSION_ADMIN = 0x2B
MATCH_MAKING_EXT = 0x32
UBI_FRIENDS = 0x2F
TITLE_STORAGE = 0x33
SEARCH_FRIENDS = 0x56
FRIEND_MESSAGES_EXT = 0x57

# --- Hermes (Ubisoft's own), ids confirmed in GRFS traffic --------------------------------
PLAYER_STATISTICS = 0x6C
RICH_PRESENCE = 0x6D
CLANS = 0x6E
MISC = 0x6F
MAP_LIST = 0x79
TRACKING_EXTENSION = 0x7D

# --- Hermes, registered by the client but not yet seen on the wire ------------------------
IP2LOCATION = 0x70
GAME_INFO = 0x71
CONTACTS_EXTENSIONS = 0x72
HERMES_ACHIEVEMENTS = 0x74
SOCIAL_NETWORKS = 0x75
GAME_STORAGE_FILE_VERSION = 0x76
GR5_AWARD = 0x77
UPLAY_WIN = 0x78
EVENTS_TRACKING = 0x7E
ONLINE_PASS = 0x7F

# --- Overlord ------------------------------------------------------------------------------
OVERLORD_RPNE = 0x1389
OVERLORD_NEWS = 0x138A
OVERLORD_CORE = 0x138B
OVERLORD_EXTRA_CONTENT = 0x138C
OVERLORD_FRIENDS = 0x138D
OVERLORD_AWARDS = 0x138E
OVERLORD_CHALLENGE = 0x138F
OVERLORD_DARE = 0x1390

# Name of each id, for logs and for finding a schema. Where the game carries no DDL for a
# protocol the name is the one its own client class uses.
NAMES = {
    NAT_TRAVERSAL: "NATTraversalProtocol",
    TICKET_GRANTING: "TicketGrantingProtocol",
    SECURE_CONNECTION: "SecureConnectionProtocol",
    NOTIFICATION: "NotificationProtocol",
    FRIENDS: "FriendsProtocol",
    MATCH_MAKING: "MatchMakingProtocol",
    PERSISTENT_STORE: "PersistentStoreProtocol",
    ACCOUNT_MANAGEMENT: "AccountManagementProtocol",
    MESSAGE_DELIVERY: "MessageDeliveryProtocol",
    UBI_ACCOUNT_MANAGEMENT: "UbiAccountManagementProtocol",
    NEWS: "NewsProtocol",
    UBI_NEWS: "UbiNewsProtocol",
    PRIVILEGES: "PrivilegesProtocol",
    TRACKING: "TrackingProtocol3",
    LOCALIZATION: "LocalizationProtocol",
    GAME_SESSION: "GameSessionProtocol",
    GAME_SESSION_ADMIN: "GameSessionAdminProtocol",
    MATCH_MAKING_EXT: "MatchMakingProtocolExt",
    UBI_FRIENDS: "UbiFriendsProtocol",
    TITLE_STORAGE: "TitleStorageProtocol",
    SEARCH_FRIENDS: "SearchFriendsProtocol",
    FRIEND_MESSAGES_EXT: "FriendMessagesExtensionServiceProtocol",
    PLAYER_STATISTICS: "PlayerStatisticsProtocol",
    RICH_PRESENCE: "RichPresenceProtocol",
    CLANS: "ClansProtocol",
    MISC: "MiscProtocol",
    MAP_LIST: "MapListProtocol",
    TRACKING_EXTENSION: "TrackingExtensionProtocol",
    IP2LOCATION: "IP2LocationProtocol",
    GAME_INFO: "GameInfoProtocol",
    CONTACTS_EXTENSIONS: "ContactsExtensionsProtocol",
    HERMES_ACHIEVEMENTS: "HermesAchievementsProtocol",
    SOCIAL_NETWORKS: "SocialNetworksProtocol",
    GAME_STORAGE_FILE_VERSION: "GameStorageFileVersionProtocol",
    GR5_AWARD: "GR5AwardProtocol",
    UPLAY_WIN: "UplayWinProtocol",
    EVENTS_TRACKING: "EventsTrackingProtocol",
    ONLINE_PASS: "OnlinePassProtocol",
    OVERLORD_RPNE: "OverlordRpneProtocol",
    OVERLORD_NEWS: "OverlordNewsProtocol",
    OVERLORD_CORE: "OverlordCoreProtocol",
    OVERLORD_EXTRA_CONTENT: "OverlordExtraContentProtocol",
    OVERLORD_FRIENDS: "OverlordFriendsProtocol",
    OVERLORD_AWARDS: "OverlordAwardsProtocol",
    OVERLORD_CHALLENGE: "OverlordChallengeProtocol",
    OVERLORD_DARE: "OverlordDareProtocol",
}
