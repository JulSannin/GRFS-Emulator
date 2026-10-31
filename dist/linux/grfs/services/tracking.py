"""TrackingProtocol3 (0x24), TrackingExtensionProtocol (0x7D), EventsTrackingProtocol (0x7E):
telemetry.

Nothing the game does depends on what these answer, but every call has to be answered: the
tracking layer resends what is not, and an error on SendUserInfo makes the client drop its
tracking id and start over. So everything is accepted, logged at debug and forgotten - this
server has no use for telemetry and keeps none of it.

TrackingProtocol3 is in the game's DDL. Its calls come over the second connection the game makes
from the same socket, signed in as "Tracking" (kerberos.TRACKING_NAME), so ctx.client is the
player's own record. SendUserInfo hands back the TrackingInformation it was sent and a tracking
id, which the client then puts on every tag (5A6120 reads both). GetConfiguration answers no
tags: the client stores the list (monitor +0xB8) but nothing was found that reads it back, so an
empty list is the neutral answer, not a known one.

TrackingExtensionProtocol and EventsTrackingProtocol are Hermes, not in the DDL; their layouts
are from the client:

* UserIsVIP (TrackingExtension 1) sends a u32 from the client's per-pad record - most probably
  the pid - printed as decimal text, and reads one bool. Nobody here is a VIP.
* StartSession (EventsTracking 1, C8A640) sends five u32 and reads one u32, the session id the
  client keeps (service +0x148) and puts second in every AddEvent. What the five inputs are is
  not known; they are logged.
* AddEvent (EventsTracking 2, C8A330) sends twenty u32, six bools and two strings, and reads
  nothing. The first u32 is the event's id and the second the session id; the rest is not known,
  and the names below say only where each value sits.
"""

import itertools

from .. import protocols
from ..rmc.router import Service


def _who(ctx) -> str:
    return ctx.client.name or ctx.client.address


class Tracking(Service):
    protocol_id = protocols.TRACKING
    protocol_name = "TrackingProtocol3"

    def __init__(self, backend):
        super().__init__(backend)
        self._ids = itertools.count(1)

    def SendTag(self, ctx, trackingID=0, tag="", attributes="", deltaTime=0):
        self.log.debug("%s: tag %s %s (id %d, +%d)", _who(ctx), tag, attributes, trackingID,
                       deltaTime)

    def SendTagAndUpdateUserInfo(self, ctx, trackingID=0, tag="", attributes="", deltaTime=0,
                                 userID=""):
        self.log.debug("%s: tag %s %s (id %d, +%d, user %s)", _who(ctx), tag, attributes,
                       trackingID, deltaTime, userID)

    def SendUserInfo(self, ctx, userInfo=None, deltaTime=0):
        """A fresh tracking id for each call: the client asks again only when it has lost the
        last one, and nothing here tells ids apart anyway."""
        tracking_id = next(self._ids)
        self.log.debug("%s: user info %s -> tracking id %d", _who(ctx), userInfo, tracking_id)
        return {"userInfo": userInfo or {"ipn": 0, "userID": "", "machineID": "",
                                         "visitorID": "", "utsVersion": ""},
                "trackingID": tracking_id}

    def GetConfiguration(self, ctx):
        return {"tags": []}

    def SendTags(self, ctx, tagData=None):
        for t in tagData or []:
            self.log.debug("%s: tag %s %s (id %d, +%d)", _who(ctx), t.get("tag"),
                           t.get("attributes"), t.get("trackingID", 0), t.get("deltaTime", 0))


class TrackingExtension(Service):
    protocol_id = protocols.TRACKING_EXTENSION
    protocol_name = "TrackingExtensionProtocol"

    schema_methods = {
        1: ("UserIsVIP", [("in", "string", "userId"), ("out", "bool", "isVIP")], None),
    }

    def UserIsVIP(self, ctx, userId=""):
        return {"isVIP": False}


# Where each of AddEvent's values sits, for the names of its parameters.
_EVENT_U32 = ["eventId", "sessionId"] + ["value%d" % n for n in range(3, 21)]
_EVENT_BOOL = ["flag%d" % n for n in range(1, 7)]
_EVENT_TEXT = ["text1", "text2"]


class EventsTracking(Service):
    protocol_id = protocols.EVENTS_TRACKING
    protocol_name = "EventsTrackingProtocol"

    schema_methods = {
        1: ("StartSession", [("in", "uint32", "value%d" % n) for n in range(1, 6)]
            + [("out", "uint32", "sessionId")], None),
        2: ("AddEvent", [("in", "uint32", n) for n in _EVENT_U32]
            + [("in", "bool", n) for n in _EVENT_BOOL]
            + [("in", "string", n) for n in _EVENT_TEXT], None),
    }

    def __init__(self, backend):
        super().__init__(backend)
        self._sessions = itertools.count(1)

    def StartSession(self, ctx, **values):
        session = next(self._sessions)
        self.log.debug("%s: events session %d (%s)", _who(ctx), session,
                       ", ".join("%d" % values.get("value%d" % n, 0) for n in range(1, 6)))
        return {"sessionId": session}

    def AddEvent(self, ctx, **values):
        self.log.debug("%s: event %d in session %d", _who(ctx), values.get("eventId", 0),
                       values.get("sessionId", 0))
