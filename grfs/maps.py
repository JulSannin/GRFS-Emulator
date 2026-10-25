"""The adversarial maps, and which of them a host may be given for a game mode.

A matchmade host asks MapListProtocol.GetMapList for the maps of the mode and map pools it
chose, picks one at random and loads it. The answer is a list of world keys - the bigfile keys
of the .wor files, one per map *and mode*: Conflict on Pipeline is a different world from Decoy
on Pipeline. A key the host does not have fails the load ("File can't be found for key"), and an
empty list ends the attempt before the lobby exists. So every key here is one the game's own map
table lists, and every question gets a non-empty answer.

The table is schema/maps.json, read out of Yeti.big by tools/extract_maps.py. The rest is the
game script's (AICLASS_PC_F.dll):

* The mask's mode bits (10453D70 turns a mode into its bit): 0x10 Conflict, 0x20 Decoy,
  0x40 Saboteur, 0x80 Siege, 0x100 Stockpile (needs the first DLC), 0x200 the sixth mode (needs
  the second). 0x3F0 is "any mode". The table's GameMode 1..6 are the same modes in that order:
  1 = 0x100, 2 = 0x10, 3 = 0x20, 4 = 0x40, 5 = 0x80, 6 = 0x200 - config-dlc1.xml names the
  Stockpile worlds _gSTO and the Conflict ones _gCON, and they are the table's modes 1 and 2.
* The mask's pool bits: 0x4000 the ten maps of the base game, 0x8000 those of the first DLC,
  0x10000 those of the second (104747CB: DLC ownership flag 2 gives 0x8000 and flag 8 gives
  0x10000; flag 2 is also the one Stockpile needs, and config-dlc1.xml ships Stockpile together
  with Riot, Skyline and Evicted). Which map is in which pool is the script's own numbering
  (100E5E50, 100E36D5): maps 0..9 are the base game's, 10..12 the first DLC's, 13..15 the
  second's - and a host whose mask has a DLC bit re-rolls up to a hundred times until it lands
  on one of 10..15 (100E5F20).
* Market (Night) and Harbor (Night), maps 16 and 17, are in the table but in no pool the mask
  can name, and their seventh mode is one the menu never offers. They are not handed out: a map
  the host might not own is worse than a shorter list.

Bits 1 and 2 of the mask are the playlist's own (1046BA20) and say nothing about maps.
"""

import json
import os

PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "schema",
                    "maps.json")

ADVERSARIAL = 2

# GameMode in the map table -> its bit in a GetMapList mask.
MODE_BITS = {1: 0x100, 2: 0x10, 3: 0x20, 4: 0x40, 5: 0x80, 6: 0x200}
ANY_MODE = 0x3F0
MODE_NAMES = {0x10: "Conflict", 0x20: "Decoy", 0x40: "Saboteur", 0x80: "Siege",
              0x100: "Stockpile", 0x200: "mode 6"}

BASE, DLC1, DLC2 = 0x4000, 0x8000, 0x10000
POOL_BITS = BASE | DLC1 | DLC2

# Name id (the Oasis id of the map's name) -> pool, as the game script numbers them.
POOLS = {
    BASE: tuple(range(0x1A0E6, 0x1A0F0)),       # Pipeline .. Alpha, maps 0..9
    DLC1: (0x1E670, 0x1E672, 0x1E673),          # Riot, Skyline, Evicted, maps 10..12
    DLC2: (0x1E674, 0x1E675, 0x1E676),          # Switchback, Palace, Transit, maps 13..15
}


class MapError(Exception):
    pass


class Map:
    __slots__ = ("name_id", "name", "keys")

    def __init__(self, name_id: int, name: str, keys: dict):
        self.name_id = name_id
        self.name = name
        self.keys = keys            # mode bit -> world key

    def __repr__(self):
        return "Map(%#x %s)" % (self.name_id, self.name)


class Maps:
    def __init__(self, path: str = PATH):
        self.path = path
        self.by_name_id = {}
        self.load()

    def load(self) -> None:
        try:
            with open(self.path, encoding="utf-8") as f:
                table = json.load(f)
        except (OSError, ValueError) as e:
            raise MapError("cannot read the map table %s: %s" % (self.path, e)) from e
        for row in table.get("maps", []):
            if row.get("type") != ADVERSARIAL:
                continue
            keys = {MODE_BITS[int(mode)]: int(found[0], 16)
                    for mode, found in row["modes"].items()
                    if int(mode) in MODE_BITS and found}
            self.by_name_id[row["name_id"]] = Map(row["name_id"], row.get("name", ""), keys)
        missing = [hex(n) for ids in POOLS.values() for n in ids if n not in self.by_name_id]
        if missing:
            raise MapError("the map table %s has no map %s" % (self.path, ", ".join(missing)))

    # ----- questions ----------------------------------------------------------------------
    def pool(self, bits: int) -> list:
        """The maps of every pool named in `bits`, in the game's own order."""
        return [self.by_name_id[n] for bit, ids in POOLS.items() if bits & bit for n in ids]

    def keys_for(self, mask: int) -> list:
        """The world keys of every map in the mask's pools, in each of its modes. Never empty:
        a mask that names no mode means any, one that names no pool means the base game's, and
        modes no map of those pools has fall back to the base game's maps."""
        modes = [bit for bit in sorted(set(MODE_BITS.values())) if mask & bit] or \
            sorted(set(MODE_BITS.values()))
        pools = mask & POOL_BITS or BASE
        keys = self._keys(self.pool(pools), modes)
        if not keys:
            keys = self._keys(self.pool(BASE), modes)
        if not keys:
            keys = self._keys(self.pool(BASE), sorted(set(MODE_BITS.values())))
        return keys

    @staticmethod
    def _keys(maps: list, modes: list) -> list:
        return [m.keys[bit] for bit in modes for m in maps if bit in m.keys]

    def describe(self, key: int) -> str:
        """'Pipeline Conflict' for a world key, for the log."""
        for m in self.by_name_id.values():
            for bit, k in m.keys.items():
                if k == key:
                    return "%s %s" % (m.name or hex(m.name_id), MODE_NAMES.get(bit, hex(bit)))
        return "%08x" % key
