"""Player statistics: where a player's multiplayer progression lives.

Future Soldier keeps no progression of its own between sessions. Rank, XP, the loadout of every
class, unlocked items and the whole career page are written to PlayerStatisticsProtocol by the
game script's profile manager and read back from it at every sign-in - nothing else stores them
(see services.player_statistics). So this file is the player's profile, and losing it, or
answering a read with an error, sends them back to rank 1: the client takes a failed read for a
lost profile, resets to defaults, and its next save overwrites whatever was stored.

A value is addressed by (pid, board, contexts, stat id), and kept per value type, because the
protocol keeps the types apart - one board may carry Int32 and Int64 stats under the same
contexts (board 2 does: XP and rank are Int32, stats 7 and 8 Int64), and a read of one type
never sees the other.

    board     a byte; which table. 1 is the profile header, 2 a class, 3 a class loadout slot,
              0xd the career, ...
    contexts  a list of u32 keys inside the board: none for board 1, {class} for board 2,
              {class, slot} for board 3. A read may put 0xFFFFFFFF in a position to mean any.
    stat id   a byte; which value of the row.

A send carries an operation with every value, which is applied here, not on the client: the
client sends a career total as an ADD of what this match earned, so a server that stored every
value as sent would replace a career with one match.

    0 ADD   1 SUB   2 SET   3 MAX   4 MIN

The codes are the client's own, from the switch in its pending-change queue (AICLASS 100AE1C0);
integers wrap at their width there and do here. The game only sends Int32 and Int64 stats; the
float, string and blob methods have no caller in its script and get the plainest reading of the
same operations.
"""

import base64
import json
import math
import os
import struct
import time

from . import log as logs

ADD, SUB, SET, MAX, MIN = range(5)
OPS = {ADD: "ADD", SUB: "SUB", SET: "SET", MAX: "MAX", MIN: "MIN"}

# A context that matches any value, in a read.
ANY = 0xFFFFFFFF

KINDS = ("int32", "int64", "float", "string", "blob")

# Text travels with a 16-bit length - a string's counts its terminating NUL - so a value that
# grew past this could never be sent back, and a read that cannot be answered costs the profile.
MAX_STRING = 0xFFFE
MAX_BLOB = 0xFFFF


def _wrap(value: int, bits: int) -> int:
    half = 1 << (bits - 1)
    return ((value + half) % (1 << bits)) - half


def _f32(value: float) -> float:
    """The value as the client would hold it: rounded to single precision, infinite when too
    large for it rather than an error."""
    try:
        return struct.unpack("<f", struct.pack("<f", value))[0]
    except OverflowError:
        return math.copysign(math.inf, value)


def _normal(kind: str, value):
    """A value as the wire type would carry it, or a ValueError."""
    if kind == "int32":
        return _wrap(int(value), 32)
    if kind == "int64":
        return _wrap(int(value), 64)
    if kind == "float":
        return _f32(float(value))
    if kind == "string":
        value = "" if value is None else str(value)
        if len(value.encode("latin-1", "replace")) > MAX_STRING:
            raise ValueError("a string of %d characters" % len(value))
        return value
    if kind == "blob":
        value = bytes(value or b"")
        if len(value) > MAX_BLOB:
            raise ValueError("a blob of %d bytes" % len(value))
        return value
    raise ValueError("no value type %r" % kind)


def apply(kind: str, old, value, op: int):
    """What a stat holds after `op` with `value`, from `old` (None when nothing is stored).

    Raises ValueError for an operation that has no meaning for this type, or a result the type
    cannot carry; the stored value then stays as it was.
    """
    value = _normal(kind, value)
    if op == SET:
        return value
    if op not in OPS:
        raise ValueError("the client's operations are 0 to 4")
    if kind in ("int32", "int64", "float"):
        if old is None:
            # Nothing to add to is zero; nothing to compare with is no bound at all.
            return _normal(kind, -value) if op == SUB else value
        if op == ADD:
            return _normal(kind, old + value)
        if op == SUB:
            return _normal(kind, old - value)
        if op == MAX:
            return value if value > old else old
        return value if value < old else old
    # Text: appended to, or compared, but nothing can be taken away from it.
    if op == SUB:
        raise ValueError("SUB on a %s" % kind)
    if old is None:
        return value
    if op == ADD:
        return _normal(kind, old + value)
    if op == MAX:
        return value if value > old else old
    return value if value < old else old


def _matches(pattern: tuple, contexts: tuple) -> bool:
    return len(pattern) == len(contexts) and all(p == ANY or p == c
                                                 for p, c in zip(pattern, contexts))


# ----- how values look in the file --------------------------------------------------------------
def _to_json(kind: str, value):
    if kind == "blob":
        return base64.b64encode(value).decode("ascii")
    if kind == "float" and not math.isfinite(value):
        return "nan" if math.isnan(value) else ("inf" if value > 0 else "-inf")
    return value


def _from_json(kind: str, value):
    if kind == "blob":
        if not isinstance(value, str):
            raise ValueError("not base64 text")
        return _normal(kind, base64.b64decode(value.encode("ascii"), validate=True))
    if kind == "float":
        return _f32(float(value))           # float("nan") and float("-inf") read the words
    if kind == "string":
        if not isinstance(value, str):
            raise ValueError("not a string")
        return _normal(kind, value)
    return _normal(kind, _int(value, -(1 << 63), (1 << 64) - 1))


def _int(value, low: int, high: int) -> int:
    """A whole number from the file, inside the range its wire field can carry. Out of range is
    an error, not wrapped: board 0x101 quietly read as board 1 would overwrite the header."""
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ValueError("%r is not a number from %d to %d" % (value, low, high))
    return value


def _row_from_json(entry) -> tuple:
    """(kind, (board, contexts), {stat id: value}) of one row of the file, or a ValueError."""
    if not isinstance(entry, dict) or entry.get("kind") not in KINDS:
        raise ValueError("not a row of a known kind")
    kind, contexts, stats = entry["kind"], entry.get("contexts"), entry.get("stats")
    if not isinstance(contexts, list) or not isinstance(stats, list):
        raise ValueError("contexts and stats must be lists")
    key = (_int(entry.get("board"), 0, 0xFF), tuple(_int(c, 0, 0xFFFFFFFF) for c in contexts))
    row = {}
    for pair in stats:
        if not isinstance(pair, list) or len(pair) != 2:
            raise ValueError("a stat is a [stat id, value] pair")
        row[_int(pair[0], 0, 0xFF)] = _from_json(kind, pair[1])
    return kind, key, row


class Stats:
    """Every player's stats, typed, in memory, and in one JSON file when there is a path."""

    def __init__(self, path: str = None):
        self.path = path
        self.log = logs.get("stats")
        # kind -> pid -> (board, contexts) -> {stat id: value}
        self.values = {kind: {} for kind in KINDS}
        self.dirty = False
        self.load()

    # ----- changing -------------------------------------------------------------------------
    def update(self, kind: str, pid: int, board: int, contexts, stat: int, value, op: int):
        """Apply one sent value. Returns what the stat holds now; raises ValueError (and changes
        nothing) for an operation this type cannot take."""
        key = (board, tuple(contexts))
        new = apply(kind, self.values[kind].get(pid, {}).get(key, {}).get(stat), value, op)
        self.values[kind].setdefault(pid, {}).setdefault(key, {})[stat] = new
        self.dirty = True
        return new

    def forget(self, pid: int) -> bool:
        """Drop everything stored for a player - their next sign-in starts a new profile."""
        found = False
        for rows in self.values.values():
            found = rows.pop(pid, None) is not None or found
        if found:
            self.dirty = True
            self.log.info("forgot the stats of 0x%X", pid)
        return found

    # ----- reading --------------------------------------------------------------------------
    def get(self, kind: str, pid: int, board: int, contexts=(), stat: int = None):
        """One stored value, or None - or, without a stat id, the row as a dict (None if absent)."""
        row = self.values[kind].get(pid, {}).get((board, tuple(contexts)))
        if row is None or stat is None:
            return dict(row) if row is not None else None
        return row.get(stat)

    def rows(self, kind: str, pid: int, board: int, pattern) -> list:
        """(contexts, row) of every stored row of this board whose contexts fit the pattern -
        the same number of keys, each equal or ANY - in context order."""
        pattern = tuple(pattern)
        found = [(contexts, row) for (b, contexts), row in self.values[kind].get(pid, {}).items()
                 if b == board and _matches(pattern, contexts)]
        return sorted(found, key=lambda item: item[0])

    def boards(self, pid: int) -> dict:
        """kind -> number of rows stored for this player, for logs and the panel."""
        return {kind: len(self.values[kind][pid]) for kind in KINDS if self.values[kind].get(pid)}

    def __len__(self):
        """How many players have anything stored."""
        return len({pid for rows in self.values.values() for pid in rows})

    # ----- storage --------------------------------------------------------------------------
    def load(self) -> None:
        if not self.path or not os.path.exists(self.path):
            return
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            players = data["players"]
            if not isinstance(players, dict):
                raise ValueError("'players' is not an object")
        except (OSError, ValueError, KeyError, TypeError) as e:
            # Starting empty and then saving would replace everyone's progress with whatever
            # defaults their games upload next. The file goes aside first, so it can be mended.
            aside = "%s.bad-%d" % (self.path, int(time.time()))
            try:
                os.replace(self.path, aside)
            except OSError:
                aside = None
            self.log.error("cannot read %s (%s) - starting without stats%s", self.path, e,
                           (", the file is kept as %s" % aside) if aside else "")
            return
        skipped = 0
        for pid_text, entries in players.items():
            for entry in entries if isinstance(entries, list) else [None]:
                try:
                    pid = _int(int(pid_text), 1, 0xFFFFFFFF)
                    kind, key, row = _row_from_json(entry)
                except (TypeError, ValueError, OverflowError):
                    skipped += 1
                    continue
                self.values[kind].setdefault(pid, {}).setdefault(key, {}).update(row)
        if skipped:
            self.log.warning("%s: %d unreadable row(s) left out", self.path, skipped)
        self.log.info("stats of %d player(s) from %s", len(self), self.path)

    def save(self) -> None:
        if not self.path or not self.dirty:
            return
        players = {}
        for kind in KINDS:
            for pid, rows in self.values[kind].items():
                for (board, contexts), row in sorted(rows.items()):
                    players.setdefault(str(pid), []).append({
                        "kind": kind, "board": board, "contexts": list(contexts),
                        "stats": [[stat, _to_json(kind, value)]
                                  for stat, value in sorted(row.items())]})
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"players": dict(sorted(players.items(), key=lambda kv: int(kv[0])))},
                          f, indent=1)
            os.replace(tmp, self.path)
        except OSError as e:
            self.log.error("cannot write %s: %s", self.path, e)
            return
        self.dirty = False
