"""PlayerStatisticsProtocol (0x6C): the player's multiplayer profile.

Rank, XP, every class's loadout, unlocked items and the career page are kept nowhere but here.
The game script's profile manager reads them all at sign-in and writes them back with SET as
they change; the end of a match adds to the career and leaderboard boards with ADD. What the
stored values mean, and how operations apply, is in grfs/stats.py.

    1..5   SendPlayerStats{Int32,Int64,Float,String,Blob}   list of groups, nothing back
    6..10  ReadPlayerStats{Int32,Int64,Float,String,Blob}   queries and pids, rows back

Not in the game's DDL - this is a Hermes protocol - so the layout below was read out of the
client: the request writers behind stubs 8F40B0..8F49E0 (group 91E8C0, entries 922C50, 922D80,
922EA0, 922FC0, 9230E0; query 9203C0, its stat id list 920460) and the reply parsers from 8F5530
(player 920820, group 923250, values 924BD0, 924CC0, 924D90, 924E70, 924F50). A float travels
as its four raw bytes; a blob with a 16-bit length.

The reads are where getting it wrong costs a player their progress:

* A read is never answered with an error. The client takes a failed read for a lost profile,
  resets to defaults and asks again - and its dirty-flag saves can then overwrite the stored
  profile with those defaults.
* A player with nothing stored gets an empty answer. No board-1 row is how the client knows a
  new player, and it then uploads its default profile.
* 0xFFFFFFFF in a query's contexts means any. The profile manager asks board 2 for {any} and
  board 3 for {any, any}, then reads each returned row's own contexts to learn which class and
  slot it is - so a wildcard comes back as one group per stored row, with its real contexts.
* One row comes back as one group, however many queries named it. The client counts board 1's
  groups and loads the header only when there is exactly one; two copies of it and the player
  is neither loaded nor new.

The sender of a send is always the caller: the layout carries no pid.
"""

from .. import protocols
from .. import stats as model
from ..rmc.ddl import describe
from ..rmc.router import Errors, RmcError, Service

# Method name suffix -> (value type on the wire, store kind)
TYPES = (("Int32", "int32", "int32"), ("Int64", "int64", "int64"), ("Float", "float", "float"),
         ("String", "string", "string"), ("Blob", "qbuffer", "blob"))


def _classes() -> list:
    out = [("StatsQuery", [("board", "byte"), ("contexts", "qlist<uint32>"),
                           ("statIds", "qlist<byte>")])]
    for suffix, wire, _ in TYPES:
        out += [
            ("StatsEntry" + suffix, [("statId", "byte"), ("value", wire), ("op", "byte")]),
            ("StatsGroup" + suffix, [("board", "byte"), ("contexts", "qlist<uint32>"),
                                     ("stats", "qlist<StatsEntry%s>" % suffix)]),
            ("StatsValue" + suffix, [("statId", "byte"), ("value", wire)]),
            ("StatsRow" + suffix, [("board", "byte"), ("contexts", "qlist<uint32>"),
                                   ("stats", "qlist<StatsValue%s>" % suffix)]),
            ("StatsPlayer" + suffix, [("pid", "uint32"), ("rows", "qlist<StatsRow%s>" % suffix)]),
        ]
    return out


def _methods() -> dict:
    out = {}
    for n, (suffix, _, _) in enumerate(TYPES):
        out[1 + n] = ("SendPlayerStats" + suffix,
                      [("in", "qlist<StatsGroup%s>" % suffix, "statGroups")], None)
        out[6 + n] = ("ReadPlayerStats" + suffix,
                      [("in", "qlist<StatsQuery>", "queries"), ("in", "qlist<uint32>", "pids"),
                       ("out", "qlist<StatsPlayer%s>" % suffix, "results")], None)
    return out


def _unique(items) -> list:
    return list(dict.fromkeys(items))


class PlayerStatistics(Service):
    protocol_id = protocols.PLAYER_STATISTICS
    protocol_name = "PlayerStatisticsProtocol"
    schema_classes = _classes()
    schema_methods = _methods()

    def __init__(self, backend):
        super().__init__(backend)
        self.stats = backend.add_store(model.Stats(backend.data_path("stats.json")))

    # ----- sending -----------------------------------------------------------------------------
    def SendPlayerStatsInt32(self, ctx, statGroups):
        return self._send(ctx, "int32", statGroups)

    def SendPlayerStatsInt64(self, ctx, statGroups):
        return self._send(ctx, "int64", statGroups)

    def SendPlayerStatsFloat(self, ctx, statGroups):
        return self._send(ctx, "float", statGroups)

    def SendPlayerStatsString(self, ctx, statGroups):
        return self._send(ctx, "string", statGroups)

    def SendPlayerStatsBlob(self, ctx, statGroups):
        return self._send(ctx, "blob", statGroups)

    def _send(self, ctx, kind, statGroups):
        """Apply every value in order and save. A value this type cannot take is left out with a
        warning and the rest still apply: failing the call would also stop the client sending
        its other types, which it chains behind this one and abandons at the first error."""
        pid = ctx.client.pid
        if not pid:
            raise RmcError(Errors.RV_NOT_AUTHENTICATED, "stats from a connection with no player")
        who = ctx.client.name or "0x%X" % pid
        applied, boards = 0, []
        for group in statGroups:
            board, contexts = group["board"], tuple(group["contexts"])
            for entry in group["stats"]:
                stat, value, op = entry["statId"], entry["value"], entry["op"]
                try:
                    now = self.stats.update(kind, pid, board, contexts, stat, value, op)
                except ValueError as e:
                    self.log.warning("%s: %s board %d %s stat %d: %s left out (%s)", who, kind,
                                     board, list(contexts), stat,
                                     model.OPS.get(op, "operation %d" % op), e)
                    continue
                applied += 1
                self.log.trace("%s: %s board %d %s stat %d %s %s -> %s", who, kind, board,
                               list(contexts), stat, model.OPS[op], describe(value), describe(now))
            boards.append(board)
        self.log.info("%s: %d %s stat(s) stored, board(s) %s", who, applied, kind,
                      ", ".join(str(b) for b in _unique(boards)) or "-")
        self.stats.save()
        return {}

    # ----- reading -----------------------------------------------------------------------------
    def ReadPlayerStatsInt32(self, ctx, queries, pids):
        return self._read(ctx, "int32", queries, pids)

    def ReadPlayerStatsInt64(self, ctx, queries, pids):
        return self._read(ctx, "int64", queries, pids)

    def ReadPlayerStatsFloat(self, ctx, queries, pids):
        return self._read(ctx, "float", queries, pids)

    def ReadPlayerStatsString(self, ctx, queries, pids):
        return self._read(ctx, "string", queries, pids)

    def ReadPlayerStatsBlob(self, ctx, queries, pids):
        return self._read(ctx, "blob", queries, pids)

    def _read(self, ctx, kind, queries, pids):
        """One entry per pid asked about, in the order asked, each with every stored row that a
        query fits and that holds a stat it names. An empty stat id list names every stat of
        the row - the game never sends one, and all of them is the least surprising reading.
        Somebody unknown, or with nothing stored, is an entry with no rows."""
        results = []
        for pid in _unique(pids):
            found = {}                     # (board, contexts) -> {stat id: value}, first seen first
            for query in queries:
                board, wanted = query["board"], _unique(query["statIds"])
                for contexts, row in self.stats.rows(kind, pid, board, query["contexts"]):
                    picked = [s for s in (wanted or sorted(row)) if s in row]
                    if picked:
                        merged = found.setdefault((board, contexts), {})
                        for s in picked:
                            merged.setdefault(s, row[s])
            results.append({"pid": pid, "rows": [
                {"board": board, "contexts": list(contexts),
                 "stats": [{"statId": s, "value": v} for s, v in values.items()]}
                for (board, contexts), values in found.items()]})
        self.log.debug("%s: %s stats of %s: %s", ctx.client.name or ctx.client.address, kind,
                       ", ".join("0x%X" % p for p in _unique(pids)) or "nobody",
                       ", ".join("%d row(s)" % len(r["rows"]) for r in results) or "-")
        return {"results": results}
