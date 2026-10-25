"""RichPresenceProtocol (0x6D): what a player is doing, as a blob only the game can read.

Hermes, not in the game's schema, so the layout is declared here. Read from the client's stubs:

    1 SetPresence(u32, qBuffer)          stub 8F2C30, request 8F2CC4 u32 + 8F2CD4 qBuffer;
                                         no results (dispatcher 8F2EE0 -> 8F5520)
    2 GetPresence(list<u32> pids)        stub 8F2D30
      -> list<{u32, bool, u32, qBuffer}> handler 8F3100 -> 8F3250 (u32 count) -> 8F3450 ->
                                         91E7C0: 481520 u32 +4, 481510 bool +8 (one byte),
                                         481520 u32 +0xC, 5A4CD0 qBuffer +0x10 (u16 + bytes)

SetPresence goes out from ContactServiceRDV::step 8CA9B0 whenever the client's presence changes;
its completion only logs ('update presence finished with %d'), so an error would cost nothing,
but there is nothing to refuse. The u32 is ContactServiceRDV+0x384 and the qBuffer the
presence arguments the client encodes itself (TaskRDV_SetPresence 912C00). What was set is what
is handed back; the blob is only checked, never changed (see the end of this docstring).

GetPresence is asked for by FIND FRIENDS (TaskRDV_SearchFriends 8CDC80) about the players a
search found. The friends list itself never asks: it requests the list with presence off
(A4C453 pushes 0), so nothing here can change what the FRIENDS tab shows.

Every pid asked about gets an entry, in the order asked. The bool is "online": the reader
(TaskRDV_GetPresence 913190) copies it to the contact's presence flag and ORs it into bit 0 of
the contact state, which is the bit the friends screen draws as online. The u32 and the blob are
what that player last set, or 0 and nothing when they are not here. Presence is kept in memory
only, and belongs to the connection that set it: a game that restarts comes back as a new client
record and starts with no presence until it sets one, rather than showing what its previous run
was doing.

The blob is not opaque to the client that *reads* it, and that reader trusts it completely.
TaskRDV_SetPresence (912C00) builds at most 255 bytes - a u32 count, then per argument a u32 id
and a tagged value - and sends them as "%02x" text. When the flag is set and the blob is not
empty, 913190 copies the blob onto a 516-byte stack buffer with no bound, decodes 255 hex pairs
into a 255-byte heap buffer, and walks the count through it with no bound either (values per
8B6950: tags 1 and 3 four bytes, 2 and 4 eight, 5 a u32 length and that many bytes, anything
else nothing). So one game's blob, handed on, can crash every game that searches for that
player. A blob that walk would not fit in is never handed on: the presence id is kept and the
blob dropped.
"""

import struct

from .. import protocols
from ..rmc.router import Service

# What the reader can hold: 255 bytes, as hex text.
MOST_PRESENCE_BYTES = 255
VALUE_SIZES = {1: 4, 2: 8, 3: 4, 4: 8}
STRING_TAG = 5


def readable(blob: bytes) -> bool:
    """Whether GetPresence's reader (913190 -> 8B6950) gets through this blob inside its buffer."""
    if not blob:
        return True
    if len(blob) % 2 or len(blob) > 2 * MOST_PRESENCE_BYTES:
        return False
    try:
        data = bytes.fromhex(blob.decode("ascii"))
    except ValueError:
        return False
    if len(data) < 4:
        return False
    (count,), at = struct.unpack_from("<I", data), 4
    for _ in range(count):
        if at + 8 > len(data):
            return False
        tag = struct.unpack_from("<I", data, at + 4)[0]
        at += 8
        if tag == STRING_TAG:
            if at + 4 > len(data):
                return False
            at += 4 + struct.unpack_from("<I", data, at)[0]
        else:
            at += VALUE_SIZES.get(tag, 0)
        if at > len(data):
            return False
    return True


class RichPresence(Service):
    protocol_id = protocols.RICH_PRESENCE
    protocol_name = "RichPresenceProtocol"

    schema_classes = [
        ("RichPresenceElement", [("m_pid", "uint32"), ("m_isOnline", "bool"),
                                 ("m_presenceId", "uint32"), ("m_presence", "qbuffer")]),
    ]
    schema_methods = {
        1: ("SetPresence", [("in", "uint32", "presenceId"), ("in", "qbuffer", "presence")],
            None),
        2: ("GetPresence", [("in", "qlist<uint32>", "pids"),
                            ("out", "qlist<RichPresenceElement>", "presences")], None),
    }

    def __init__(self, backend):
        super().__init__(backend)
        self.presence = {}          # pid -> (client record that set it, u32, blob)

    def SetPresence(self, ctx, presenceId, presence):
        if not ctx.client.pid:
            # Nothing to file it under; the client ignores the answer either way.
            self.log.info("%s set a presence before logging in", ctx.client.address)
            return None
        blob = bytes(presence or b"")
        if not readable(blob):
            self.log.warning("%s set a presence blob no game could read back (%d bytes: %s) -"
                             " keeping %d without it", ctx.client.name, len(blob),
                             blob[:32].hex(" "), presenceId)
            blob = b""
        self.presence[ctx.client.pid] = (ctx.client, presenceId, blob)
        self.log.debug("%s is now %d / %s", ctx.client.name, presenceId,
                       blob.decode("ascii") or "-")
        return None

    def GetPresence(self, ctx, pids):
        rows = []
        for pid in pids or []:
            online = self.backend.is_online(pid)
            presence_id, blob = 0, b""
            stored = self.presence.get(pid)
            if online and stored is not None and stored[0] is self.backend.clients.by_pid(pid):
                _, presence_id, blob = stored
            rows.append({"m_pid": pid, "m_isOnline": online, "m_presenceId": presence_id,
                         "m_presence": blob})
        self.log.info("%s asked the presence of %d player(s), %d online",
                      ctx.client.name or ctx.client.address, len(rows),
                      sum(r["m_isOnline"] for r in rows))
        return {"presences": rows}
