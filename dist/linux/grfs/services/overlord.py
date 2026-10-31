"""Overlord (0x1389..0x1390): Ubisoft's post-login layer, which the game carries no DDL for.

Overlord opens no connection of its own. Its protocols ride the player's secure connection, with
ids past 0x7F written as 0xFF and a u16, and the layouts below were read out of the client's own
stubs - there are seven of them in the whole binary, one per method declared here:

    Core     0x138B  1 GetSettings       the first step of the Overlord login
    Friends  0x138D  1 SyncFriends       login step 4, only when SKIP_FRIENDS_SYNC is false
    News     0x138A  1 GetNews           the main menu's news, and the "P2P news"
    Rpne     0x1389  1 SendEvent         script events, and sending a dare to another player
    Dare     0x1390  1 RequestDareInfo   the dares screen      2 DeleteDare
    Awards   0x138E  4 PostAwards        login step 3; unreachable on PC, kept for the layout

Why it matters at all: right after the RDV login the game runs JobOverlordPostLogin - settings,
locale, awards, friends, in that order - and if any step fails the whole Overlord object is
destroyed for the session. News, dares and these events go with it. Nothing else depends on
it, so the cost of getting it wrong is quiet, which is exactly why it is easy to miss.

The settings are what keep that login short. Each step reads one key from them through a lookup
that insists on a bool, and skips itself when the key is true. The client presets
SKIP_AWARDS_SYNC and SKIP_FRIENDS_SYNC to true before it asks, but not SKIP_LOCAL_CODE_SYNC, so an
empty answer still costs a LocalizationProtocol call. The server's keys win over the client's
when the two are merged, so all three are answered true here and the login is one round trip.
"""

import datetime
import itertools

from .. import protocols
from ..rmc.ddl import describe
from ..rmc.router import Service
from ..rmc.stream import pack_datetime

# A Variant on the wire is a tag byte and a value: 0 null, 1 int64, 2 double, 3 bool, 4 String,
# 5 datetime, 6 uint64. grfs.rmc.ddl's "variant" type is exactly that, and writes a Python bool
# with tag 3 - the only tag the settings lookup (802370 -> 8022A0, type 3) accepts.
SETTINGS = {
    "SKIP_LOCAL_CODE_SYNC": True,
    "SKIP_AWARDS_SYNC": True,
    "SKIP_FRIENDS_SYNC": True,
}

# Notification types for Overlord are grouped by thousands: the Dares client keeps every event
# whose type / 1000 is 5001, the Rpne client 5000 (804C2D, 804E5D). The client never looks at the
# remainder, so which one Ubisoft used for "a dare arrived" cannot be told from here.
DARE_RECEIVED = 5001001

# Event name and keys bSendChallenge (A677D0) puts in an Rpne event, all as int64 Variants.
SEND_DARE = "GDE"

# The far end of a news item's life, for one that should not expire.
NEVER = pack_datetime(datetime.datetime(2100, 1, 1))


class OverlordCore(Service):
    protocol_id = protocols.OVERLORD_CORE
    protocol_name = "OverlordCoreProtocol"
    schema_methods = {
        1: ("GetSettings", [("out", "std_map<string,variant>", "settings")], None),
    }

    def GetSettings(self, ctx):
        """The whole post-login, as far as the server is concerned. _OSDK_VERSION and
        OVERLORD_VERSION are also looked up (validateVersions 807680), but the result of that
        comparison is thrown away, so they are left out rather than guessed."""
        return {"settings": list(SETTINGS.items())}


class OverlordFriends(Service):
    protocol_id = protocols.OVERLORD_FRIENDS
    protocol_name = "OverlordFriendsProtocol"
    schema_methods = {
        # The u32 is a constant 0 (806A07); the list is the pids of the player's friends as the
        # game knows them.
        1: ("SyncFriends", [("in", "uint32", "uiReserved"),
                            ("in", "std_list<uint32>", "lstFriends")], None),
    }

    def SyncFriends(self, ctx, uiReserved=0, lstFriends=()):
        """Only sent if GetSettings says SKIP_FRIENDS_SYNC is false, which it does not. The
        friends this server knows are the ones FriendsProtocol keeps; nothing here is merged
        into them, because a list the game sends cannot say which side asked whom."""
        known = [self.backend.accounts.name_for(pid) or "0x%X" % pid for pid in lstFriends]
        self.log.info("%s's game lists %d friend(s): %s", ctx.client.name or ctx.client.address,
                      len(known), ", ".join(known) or "-")
        return {}


class OverlordNews(Service):
    """The news panel. Empty unless news_title or news_body is set, then one message.

    The request names a predicate class in an any<Data,string> that nothing here needs to
    understand - there is only ever the one message - so it is decoded as raw bytes and left.
    """

    protocol_id = protocols.OVERLORD_NEWS
    protocol_name = "OverlordNewsProtocol"
    schema_methods = {
        1: ("GetNews", [("in", "any<Data,string>", "oPredicate"),
                        ("in", "ResultRange", "oRange"),
                        ("out", "qlist<NewsMessage>", "lstNews")], None),
    }

    def GetNews(self, ctx, oPredicate=None, oRange=None):
        news = self._news(ctx)
        offset, size = 0, len(news)
        if isinstance(oRange, dict):
            offset = oRange.get("m_uiOffset", 0) or 0
            size = oRange.get("m_uiSize", 0) or len(news)
        return {"lstNews": news[offset:offset + size]}

    def _news(self, ctx) -> list:
        title = self.backend.config.get("news_title")
        body = self.backend.config.get("news_body")
        if not (title or body):
            return []
        published = pack_datetime(datetime.datetime.fromtimestamp(self.backend.started,
                                                                  datetime.timezone.utc))
        return [{"m_ID": 1, "m_recipientID": ctx.client.pid, "m_recipientType": 0,
                 "m_publisherPID": self.backend.server_pid, "m_publisherName": "GRFS",
                 "m_publicationTime": published, "m_displayTime": published,
                 "m_expirationTime": NEVER, "m_title": title, "m_link": "", "m_body": body}]


class OverlordRpne(Service):
    """Events. Two things send them, both through TaskOverlordSendEvent:

    * SendOVLEvent (A675F0) - a script's own event, with string values under p1, p2 and p3.
      Telemetry, as far as anything here can tell; it is logged.
    * bSendChallenge (A677D0) - a dare sent to another player: event "GDE" with targetPID,
      dareId and score. That one is passed on to the target, see OverlordDare.
    """

    protocol_id = protocols.OVERLORD_RPNE
    protocol_name = "OverlordRpneProtocol"
    schema_methods = {
        1: ("SendEvent", [("in", "string", "strEventName"),
                          ("in", "std_map<string,variant>", "mapContent")], None),
    }

    def SendEvent(self, ctx, strEventName="", mapContent=()):
        content = dict(mapContent or ())
        if strEventName == SEND_DARE:
            dares = self.backend.router.service_for(protocols.OVERLORD_DARE)
            dares.send(ctx, content)
        else:
            self.log.info("%s: event %s %s", ctx.client.name or ctx.client.address,
                          strEventName or "(unnamed)", describe(content))
        return {}


class OverlordDare(Service):
    """Dares ("challenges" on screen) between players.

    What the client does with each half was read from it, and it is less than the shape
    suggests:

    * A dare arrives as a NotificationEvent whose type is in 5,001,000-5,001,999. The Dares
      client queues it and hands param1, param2 and param3 to the script (A66CF0 -> A66C10).
      Which of the sender's three values goes where is not visible in native code; here it is
      the sender's pid, the dare id and the score, in the order bSendChallenge put them in.
    * RequestDareInfo's list is walked and dropped: the callback (A66E80) only reports that the
      call finished. So what the list holds cannot break anything; it is the dares this server
      is still holding for the player, for whatever reads them.

    Dares live in memory only. A dare for a player who is not connected is kept for the list,
    but the game has no way to show it - the notification was the only path to the screen - so
    there is nothing to gain from keeping them across a restart.
    """

    protocol_id = protocols.OVERLORD_DARE
    protocol_name = "OverlordDareProtocol"
    schema_classes = [
        ("ProtoDareInfo", [("m_id", "uint32"), ("m_fromPid", "uint32"),
                           ("m_sent", "datetime"), ("m_dareId", "uint32"),
                           ("m_score", "uint32"), ("m_state", "uint32")]),
    ]
    schema_methods = {
        1: ("RequestDareInfo", [("out", "datetime", "dtNow"),
                                ("out", "std_list<ProtoDareInfo>", "lstDares")], None),
        2: ("DeleteDare", [("in", "uint32", "uiDareId")], None),
    }

    # Held per player; past this the oldest go, so a script that loops cannot grow it forever.
    KEEP = 50

    def __init__(self, backend):
        super().__init__(backend)
        self.held = {}                       # target pid -> [ProtoDareInfo dict]
        self._ids = itertools.count(1)

    def RequestDareInfo(self, ctx):
        return {"dtNow": pack_datetime(), "lstDares": list(self.held.get(ctx.client.pid, []))}

    def DeleteDare(self, ctx, uiDareId):
        """Accepts either id - this server's, from the list, or the game's own dareId - since
        which one the script passes back is not known. Deleting what is not there is not an
        error: the game cannot have known."""
        mine = self.held.get(ctx.client.pid, [])
        keep = [d for d in mine if uiDareId not in (d["m_id"], d["m_dareId"])]
        if len(keep) != len(mine):
            self.log.info("%s deleted dare %d", ctx.client.name, uiDareId)
        self.held[ctx.client.pid] = keep
        return {}

    # ----- from OverlordRpne -----------------------------------------------------------------
    def send(self, ctx, content: dict) -> None:
        target = _int(content.get("targetPID"))
        sender = ctx.client
        # A dare that goes nowhere is still answered with success: the event is void either
        # way, and an error would only make the sender's script report a failure it cannot act
        # on.
        if not target or not self.backend.accounts.name_for(target):
            self.log.info("%s sent a dare to pid 0x%X, who has no account here",
                          sender.name or sender.address, target)
            return
        if target == sender.pid:
            self.log.info("%s dared themselves; nobody is told", sender.name)
            return
        dare = {"m_id": next(self._ids), "m_fromPid": sender.pid, "m_sent": pack_datetime(),
                "m_dareId": _int(content.get("dareId")) & 0xFFFFFFFF,
                "m_score": _int(content.get("score")) & 0xFFFFFFFF, "m_state": 0}
        held = self.held.setdefault(target, [])
        held.append(dare)
        del held[:-self.KEEP]
        told = self.backend.notify(target, DARE_RECEIVED, source=sender.pid,
                                   param1=sender.pid, param2=dare["m_dareId"],
                                   param3=dare["m_score"])
        self.log.info("%s dared %s (dare %d, score %d)%s", sender.name,
                      self.backend.accounts.name_for(target), dare["m_dareId"], dare["m_score"],
                      "" if told else " - not connected, so it waits in the list")


class OverlordAwards(Service):
    """Awards. Seven methods in the client's dispatcher, one stub, and that one never runs here.

    m4 is JobSyncAwards::postAwards (804E90 from 8072B0): it posts the awards the game holds
    locally. It runs only when GetSettings leaves SKIP_AWARDS_SYNC false, and even then the
    list it filters is never filled on PC - the call at 80745A is unreachable. m1-m3 and m5-m7
    have no stub at all. The handler is here so the layout is on record and a call, should one
    ever come, is answered rather than failed.
    """

    protocol_id = protocols.OVERLORD_AWARDS
    protocol_name = "OverlordAwardsProtocol"
    schema_classes = [
        # Built from ProtoAwardPlatform: two u32 at +0x10 and +0x14 (an award id and a platform
        # or progress value, most likely) and a datetime (801170).
        ("ProtoAwardPlatform", [("m_award", "uint32"), ("m_value", "uint32"),
                                ("m_date", "datetime")]),
    ]
    schema_methods = {
        4: ("PostAwards", [("in", "std_list<ProtoAwardPlatform>", "lstAwards")], None),
    }

    def PostAwards(self, ctx, lstAwards=()):
        self.log.info("%s posted %d award(s): %s", ctx.client.name or ctx.client.address,
                      len(lstAwards), ", ".join(str(a.get("m_award")) for a in lstAwards) or "-")
        return {}


def _int(value) -> int:
    """A Variant's value as an int: dares come as int64, a script event's as strings."""
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0
