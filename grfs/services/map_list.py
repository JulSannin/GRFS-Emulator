"""MapListProtocol (0x79): which game modes and maps a matchmade host may use.

    1 GetAvailableMapCategories   the front end, before the game-mode menu is built
    2 GetMapList                  a matchmade host, twice per attempt, before the lobby exists

Hosting a matchmade lobby dies here when this is wrong. The host asks GetMapList for its mask,
picks a map at random from the answer, then asks again with the full mask and keeps or re-picks;
an empty second answer drops it back to the online menu and an error drops it to the start, in
both cases without a lobby and without ever asking for the match key. So GetMapList always
answers with at least one real world key - see grfs/maps.py for where they come from and what
the mask means.

Not in the game's DDL - this is a Hermes protocol - so the layout was read out of the client:
request stubs 8E4430 (nothing) and 8E4530 (one u32), both replies parsed by 5AFBA0 as a list of
u32 (a u32 count, then the values).

A category is a game mode's bit with the pool and playlist bits it allows (1046B960): the menu
offers a mode only when some category carries its bit, the client keeps only the pool bits the
player owns a DLC for, and a session whose bits are not all in the category for its mode has
them rewritten (1046BA20). So each mode comes with every bit the client knows - 1 and 2 of the
playlist, 0x4000 0x8000 0x10000 of the pools - which leaves the choice to the client, as with the
live service. Modes without a DLC the player owns are dropped by the client itself.
"""

from .. import maps as model
from .. import protocols
from ..rmc.router import Errors, RmcError, Service

PLAYLIST_BITS = 0x1 | 0x2
CATEGORIES = [bit | PLAYLIST_BITS | model.POOL_BITS
              for bit in sorted(set(model.MODE_BITS.values()))]


class MapList(Service):
    protocol_id = protocols.MAP_LIST
    protocol_name = "MapListProtocol"

    schema_methods = {
        1: ("GetAvailableMapCategories", [("out", "qlist<uint32>", "categories")], None),
        2: ("GetMapList", [("in", "uint32", "categoryId"), ("out", "qlist<uint32>", "mapIds")],
            None),
    }

    def __init__(self, backend):
        super().__init__(backend)
        try:
            self.maps = model.Maps()
        except model.MapError as e:
            # Everything else still works; hosting a matchmade lobby will not.
            self.log.error("%s - GetMapList will fail and no matchmade lobby can be hosted", e)
            self.maps = None

    def GetAvailableMapCategories(self, ctx):
        return {"categories": CATEGORIES}

    def GetMapList(self, ctx, categoryId):
        if self.maps is None:
            raise RmcError(Errors.CORE_UNKNOWN, "no map table")
        keys = self.maps.keys_for(categoryId)
        self.log.info("%s may host %d world(s) for mask %#x, e.g. %s",
                      ctx.client.name or ctx.client.address, len(keys), categoryId,
                      self.maps.describe(keys[0]))
        return {"mapIds": keys}
