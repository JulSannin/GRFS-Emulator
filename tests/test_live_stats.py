"""PlayerStatisticsProtocol against a running server: the player's profile, kept and read back."""

import json
import math
import os
import shutil
import struct
import tempfile
import unittest

from grfs import stats as model
from grfs.harness import Harness

from tests.live import LiveTest

S = "PlayerStatisticsProtocol"
ANY = model.ANY
ADD, SUB, SET, MAX, MIN = model.ADD, model.SUB, model.SET, model.MAX, model.MIN
SUFFIX = {"int32": "Int32", "int64": "Int64", "float": "Float", "string": "String",
          "blob": "Blob"}


def send(player, kind, *groups):
    """groups: (board, contexts, [(stat, value, op), ...])"""
    return player.call(S, "SendPlayerStats" + SUFFIX[kind], {"statGroups": [
        {"board": board, "contexts": list(contexts),
         "stats": [{"statId": s, "value": v, "op": op} for s, v, op in entries]}
        for board, contexts, entries in groups]})


def read(player, kind, queries, pids):
    """queries: (board, contexts, stat ids). Returns {pid: [(board, contexts, {stat: value})]}
    in the order the server answered."""
    reply = player.call(S, "ReadPlayerStats" + SUFFIX[kind], {"queries": [
        {"board": board, "contexts": list(contexts), "statIds": list(ids)}
        for board, contexts, ids in queries], "pids": list(pids)})
    return {r["pid"]: [(row["board"], tuple(row["contexts"]),
                        {e["statId"]: e["value"] for e in row["stats"]}) for row in r["rows"]]
            for r in reply["results"]}


def value(player, kind, board, contexts, stat):
    rows = read(player, kind, [(board, contexts, [stat])], [player.pid])[player.pid]
    return rows[0][2][stat] if rows else None


class Operations(LiveTest):
    def setUp(self):
        super().setUp()
        self.alice = self.player("alice")

    def test_a_new_player_reads_nothing_of_any_type(self):
        for kind in SUFFIX:
            self.assertEqual(read(self.alice, kind, [(1, (), [1, 2, 3]), (2, (ANY,), [1, 4])],
                                  [self.alice.pid]), {self.alice.pid: []}, kind)

    def test_every_operation_on_int32(self):
        a = self.alice
        steps = [(ADD, 5, 5),            # adding to nothing starts from zero
                 (ADD, 10, 15), (SUB, 3, 12), (SET, 100, 100),
                 (MAX, 50, 100), (MAX, 150, 150), (MIN, 200, 150), (MIN, -7, -7),
                 (SET, 0x7FFFFFFF, 0x7FFFFFFF), (ADD, 1, -0x80000000)]   # wraps, as the client
        for op, v, expected in steps:
            send(a, "int32", (0xd, (1,), [(4, v, op)]))
            self.assertEqual(value(a, "int32", 0xd, (1,), 4), expected, (op, v))
        send(a, "int32", (0xd, (1,), [(5, 9, SUB)]))
        self.assertEqual(value(a, "int32", 0xd, (1,), 5), -9)
        send(a, "int32", (0xd, (1,), [(6, 9, MIN)]))
        self.assertEqual(value(a, "int32", 0xd, (1,), 6), 9)

    def test_every_operation_on_int64(self):
        a = self.alice
        big = 1 << 40
        steps = [(SET, big, big), (ADD, big, 2 * big), (SUB, 1, 2 * big - 1),
                 (MAX, big, 2 * big - 1), (MAX, 3 * big, 3 * big), (MIN, big, big),
                 (SET, (1 << 63) - 1, (1 << 63) - 1), (ADD, 2, -(1 << 63) + 1)]
        for op, v, expected in steps:
            send(a, "int64", (2, (3,), [(7, v, op)]))
            self.assertEqual(value(a, "int64", 2, (3,), 7), expected, (op, v))

    def test_every_operation_on_float(self):
        a = self.alice
        f32 = lambda x: struct.unpack("<f", struct.pack("<f", x))[0]       # noqa: E731
        steps = [(SET, 1.5, 1.5), (ADD, 0.25, 1.75), (SUB, 2.0, -0.25), (MAX, -1.0, -0.25),
                 (MAX, 8.0, 8.0), (MIN, 9.0, 8.0), (MIN, 0.5, 0.5), (SET, 0.1, f32(0.1))]
        for op, v, expected in steps:
            send(a, "float", (9, (1,), [(1, v, op)]))
            self.assertEqual(value(a, "float", 9, (1,), 1), expected, (op, v))

    def test_every_operation_on_strings(self):
        a = self.alice
        steps = [(SET, "Kozak", "Kozak"), (ADD, " Ghost", "Kozak Ghost"),
                 (MAX, "Alpha", "Kozak Ghost"), (MAX, "Zulu", "Zulu"), (MIN, "Bravo", "Bravo"),
                 (SET, "", "")]
        for op, v, expected in steps:
            send(a, "string", (0x14, (1,), [(2, v, op)]))
            self.assertEqual(value(a, "string", 0x14, (1,), 2), expected, (op, v))

    def test_every_operation_on_blobs(self):
        a = self.alice
        steps = [(SET, b"\x00\x01", b"\x00\x01"), (ADD, b"\xff", b"\x00\x01\xff"),
                 (MAX, b"\x00", b"\x00\x01\xff"), (MAX, b"\x02", b"\x02"),
                 (MIN, b"\x01\x00", b"\x01\x00"), (SET, bytes(range(256)), bytes(range(256)))]
        for op, v, expected in steps:
            send(a, "blob", (3, (0, 1), [(9, v, op)]))
            self.assertEqual(value(a, "blob", 3, (0, 1), 9), expected, (op, v))

    def test_what_a_type_cannot_take_is_left_out_and_the_rest_applies(self):
        a = self.alice
        send(a, "string", (0x14, (1,), [(2, "keep", SET)]))
        send(a, "string", (0x14, (1,), [(2, "gone", SUB), (3, "kept", SET)]))
        self.assertEqual(read(a, "string", [(0x14, (1,), [2, 3])], [a.pid]),
                         {a.pid: [(0x14, (1,), {2: "keep", 3: "kept"})]})
        self.assertTrue(self.logged("SUB on a string"))
        send(a, "int32", (1, (), [(1, 7, 9), (2, 8, SET)]))       # no operation 9
        self.assertEqual(read(a, "int32", [(1, (), [1, 2])], [a.pid]),
                         {a.pid: [(1, (), {2: 8})]})
        self.assertTrue(self.logged("operation 9"))

    def test_the_types_are_kept_apart(self):
        a = self.alice
        send(a, "int32", (2, (0,), [(1, 1200, SET)]))
        send(a, "int64", (2, (0,), [(1, 1 << 50, SET)]))
        self.assertEqual(value(a, "int32", 2, (0,), 1), 1200)
        self.assertEqual(value(a, "int64", 2, (0,), 1), 1 << 50)
        self.assertIsNone(value(a, "float", 2, (0,), 1))

    def test_one_send_of_several_groups_and_boards(self):
        a = self.alice
        send(a, "int32", (1, (), [(1, 1, SET), (2, 2, SET), (3, 3, SET)]),
             (2, (0,), [(1, 500, SET), (4, 3, SET)]),
             (3, (0, 1), [(0xa, 11, SET)]),
             (1, (), [(1, 4, ADD)]))                  # later in the same call, applied after
        self.assertEqual(read(a, "int32", [(1, (), [1, 2, 3]), (2, (0,), [1, 4]),
                                           (3, (0, 1), [0xa])], [a.pid]),
                         {a.pid: [(1, (), {1: 5, 2: 2, 3: 3}), (2, (0,), {1: 500, 4: 3}),
                                  (3, (0, 1), {0xa: 11})]})


class Reading(LiveTest):
    def setUp(self):
        super().setUp()
        self.alice = self.player("alice")
        self.bob = self.player("bob")
        # What the profile manager uploads for a new player: the header, a row per class, a row
        # per class and loadout slot.
        send(self.alice, "int32", (1, (), [(1, 1, SET), (2, 0, SET), (3, 0, SET)]),
             *[(2, (c,), [(1, 100 * c, SET), (4, c, SET)]) for c in (2, 0, 1)],
             *[(3, (c, s), [(0xa, 10 * c + s, SET)]) for c in (0, 1) for s in (1, 0)])

    def test_a_wildcard_comes_back_as_each_stored_row(self):
        a = self.alice
        self.assertEqual(read(a, "int32", [(2, (ANY,), [1, 4])], [a.pid]), {a.pid: [
            (2, (0,), {1: 0, 4: 0}), (2, (1,), {1: 100, 4: 1}), (2, (2,), {1: 200, 4: 2})]})
        self.assertEqual(read(a, "int32", [(3, (ANY, ANY), [0xa])], [a.pid]), {a.pid: [
            (3, (0, 0), {0xa: 0}), (3, (0, 1), {0xa: 1}),
            (3, (1, 0), {0xa: 10}), (3, (1, 1), {0xa: 11})]})
        # Part of the key given, part left open.
        self.assertEqual(read(a, "int32", [(3, (1, ANY), [0xa])], [a.pid]), {a.pid: [
            (3, (1, 0), {0xa: 10}), (3, (1, 1), {0xa: 11})]})
        self.assertEqual(read(a, "int32", [(3, (ANY, 1), [0xa])], [a.pid]), {a.pid: [
            (3, (0, 1), {0xa: 1}), (3, (1, 1), {0xa: 11})]})

    def test_contexts_match_by_count_as_well_as_value(self):
        a = self.alice
        self.assertEqual(read(a, "int32", [(3, (ANY,), [0xa]), (2, (ANY, ANY), [1]),
                                           (2, (), [1])], [a.pid]), {a.pid: []})
        # A query without contexts finds the row without contexts, and nothing else.
        self.assertEqual(read(a, "int32", [(1, (), [1])], [a.pid]), {a.pid: [(1, (), {1: 1})]})
        self.assertEqual(read(a, "int32", [(2, (7,), [1])], [a.pid]), {a.pid: []})

    def test_only_the_stats_asked_for_and_only_rows_that_hold_one(self):
        a = self.alice
        self.assertEqual(read(a, "int32", [(2, (ANY,), [4, 99])], [a.pid]), {a.pid: [
            (2, (0,), {4: 0}), (2, (1,), {4: 1}), (2, (2,), {4: 2})]})
        self.assertEqual(read(a, "int32", [(2, (ANY,), [99])], [a.pid]), {a.pid: []})
        # No stat ids at all reads the whole row.
        self.assertEqual(read(a, "int32", [(2, (1,), [])], [a.pid]),
                         {a.pid: [(2, (1,), {1: 100, 4: 1})]})

    def test_board_one_comes_back_as_one_row_however_it_is_asked(self):
        a = self.alice
        rows = read(a, "int32", [(1, (), [1]), (1, (), [2]), (1, (), [3, 1])], [a.pid])[a.pid]
        self.assertEqual(rows, [(1, (), {1: 1, 2: 0, 3: 0})])
        # Overlapping queries of one board make one row per stored row too.
        rows = read(a, "int32", [(2, (ANY,), [1]), (2, (1,), [4])], [a.pid])[a.pid]
        self.assertEqual(rows, [(2, (0,), {1: 0}), (2, (1,), {1: 100, 4: 1}), (2, (2,), {1: 200})])

    def test_several_players_in_one_read(self):
        send(self.bob, "int32", (1, (), [(1, 9, SET)]), (2, (4,), [(1, 4000, SET)]))
        nobody = 0x7777
        got = read(self.bob, "int32", [(1, (), [1]), (2, (ANY,), [1])],
                   [self.alice.pid, nobody, self.bob.pid, self.alice.pid])
        self.assertEqual(list(got), [self.alice.pid, nobody, self.bob.pid])
        self.assertEqual(got[self.alice.pid], [(1, (), {1: 1}), (2, (0,), {1: 0}),
                                               (2, (1,), {1: 100}), (2, (2,), {1: 200})])
        self.assertEqual(got[nobody], [])
        self.assertEqual(got[self.bob.pid], [(1, (), {1: 9}), (2, (4,), {1: 4000})])

    def test_another_players_stats_are_readable_and_their_own(self):
        # Bob opens Alice's career card; what Bob sends never lands on Alice.
        send(self.bob, "int32", (1, (), [(1, 50, ADD)]))
        self.assertEqual(read(self.bob, "int32", [(1, (), [1])], [self.alice.pid]),
                         {self.alice.pid: [(1, (), {1: 1})]})
        self.assertEqual(value(self.bob, "int32", 1, (), 1), 50)
        self.assertEqual(value(self.alice, "int32", 1, (), 1), 1)
        # A third player, signed in later, sees both.
        carol = self.player("carol")
        got = read(carol, "int32", [(1, (), [1])], [self.alice.pid, self.bob.pid, carol.pid])
        self.assertEqual(got, {self.alice.pid: [(1, (), {1: 1})],
                               self.bob.pid: [(1, (), {1: 50})], carol.pid: []})

    def test_an_empty_read(self):
        self.assertEqual(read(self.alice, "int32", [], [self.alice.pid]), {self.alice.pid: []})
        self.assertEqual(read(self.alice, "int32", [(1, (), [1])], []), {})


class SignIn(LiveTest):
    """The profile manager's own sequence (100AAB40, 100AB380, 100C1090), board for board."""

    INT32 = [(1, (), [1, 2, 3]), (2, (ANY,), [1, 2, 3, 4, 5]),
             (3, (ANY, ANY), [1, 2, 3, 4, 5, 8, 9, 0xa, 0xb, 0xc]), (0xd, (), list(range(1, 0x2b)))]
    INT64 = [(2, (ANY,), [7, 8]), (3, (ANY, ANY), [6, 7]), (4, (ANY, ANY), [1])]

    def sign_in(self, p):
        return read(p, "int32", self.INT32, [p.pid])[p.pid], read(p, "int64", self.INT64,
                                                                   [p.pid])[p.pid]

    def test_a_new_player_uploads_and_is_found_next_time(self):
        a = self.player("alice")
        self.assertEqual(self.sign_in(a), ([], []))           # no board-1 row: a new profile
        send(a, "int32", (1, (), [(1, 1, SET), (2, 0, SET), (3, 0, SET)]),
             *[(2, (c,), [(s, 0, SET) for s in (1, 2, 3, 4, 5)]) for c in range(6)],
             *[(3, (c, s), [(0xa, 0, SET), (0xc, 0, SET), (0xb, 0, SET)])
               for c in range(6) for s in (0, 1)])
        send(a, "int64", *[(2, (c,), [(7, 0, SET), (8, 0, SET)]) for c in range(6)],
             (4, (2, 0x51), [(1, 1 << 40, SET)]))
        int32, int64 = self.sign_in(a)
        self.assertEqual([r for r in int32 if r[0] == 1], [(1, (), {1: 1, 2: 0, 3: 0})])
        self.assertEqual([r[1] for r in int32 if r[0] == 2], [(c,) for c in range(6)])
        self.assertEqual([r[1] for r in int32 if r[0] == 3],
                         [(c, s) for c in range(6) for s in (0, 1)])
        self.assertEqual(int64, [(2, (c,), {7: 0, 8: 0}) for c in range(6)]
                         + [(4, (2, 0x51), {1: 1 << 40})])
        # A match: XP for the class with SET, the career total with ADD (twice: two matches).
        for xp in (1500, 2600):
            send(a, "int32", (2, (3,), [(1, xp, SET), (4, 2, SET)]))
            send(a, "int32", (0xd, (), [(4, 7, ADD), (5, 1, ADD)]))
        int32, _ = self.sign_in(a)
        self.assertIn((2, (3,), {1: 2600, 2: 0, 3: 0, 4: 2, 5: 0}), int32)
        self.assertIn((0xd, (), {4: 14, 5: 2}), int32)
        # And Bob, opening Alice's card, sees the same career.
        bob = self.player("bob")
        self.assertEqual(read(bob, "int32", [(0xd, (), [4, 5])], [a.pid]),
                         {a.pid: [(0xd, (), {4: 14, 5: 2})]})


class OnTheWire(LiveTest):
    def test_the_bytes_of_a_read(self):
        """The layout the client's parsers read (920820, 923250, 924BD0), byte for byte."""
        a = self.player("alice")
        send(a, "int32", (2, (1,), [(4, 3, SET), (1, -2, SET)]))
        request = (struct.pack("<I", 1) + bytes([2]) + struct.pack("<II", 1, ANY)
                   + struct.pack("<I", 2) + bytes([1, 4]) + struct.pack("<II", 1, a.pid))
        reply = a.call_raw(S, 6, request)
        self.assertTrue(reply.success)
        self.assertEqual(reply.body, struct.pack("<III", 1, a.pid, 1) + bytes([2])
                         + struct.pack("<III", 1, 1, 2) + bytes([1]) + struct.pack("<i", -2)
                         + bytes([4]) + struct.pack("<I", 3))

    def test_the_bytes_of_a_send_and_its_empty_answer(self):
        """Group 91E8C0, entry 922D80: byte, u64, byte."""
        a = self.player("alice")
        request = (struct.pack("<I", 1) + bytes([2]) + struct.pack("<II", 1, 5)
                   + struct.pack("<I", 1) + bytes([7]) + struct.pack("<Q", 1 << 33) + bytes([SET]))
        reply = a.call_raw(S, 2, request)
        self.assertTrue(reply.success)
        self.assertEqual(reply.body, b"")
        self.assertEqual(value(a, "int64", 2, (5,), 7), 1 << 33)

    def test_a_float_travels_as_its_four_bytes_and_a_blob_with_a_short_length(self):
        a = self.player("alice")
        send(a, "float", (9, (1,), [(1, 2.5, SET)]))
        send(a, "blob", (9, (1,), [(1, b"ab", SET)]))
        query = (struct.pack("<I", 1) + bytes([9]) + struct.pack("<II", 1, 1)
                 + struct.pack("<I", 1) + bytes([1]) + struct.pack("<II", 1, a.pid))
        head = struct.pack("<III", 1, a.pid, 1) + bytes([9]) + struct.pack("<III", 1, 1, 1)
        self.assertEqual(a.call_raw(S, 8, query).body, head + bytes([1]) + struct.pack("<f", 2.5))
        self.assertEqual(a.call_raw(S, 10, query).body, head + bytes([1]) + b"\x02\x00ab")

    def test_a_send_too_long_for_one_packet(self):
        """A whole profile upload: six classes, every item - several fragments each way."""
        a = self.player("alice")
        groups = [(4, (c, item), [(1, item * 3 + c, SET), (2, 1, SET)])
                  for c in range(6) for item in range(40)]
        send(a, "int32", *groups)
        self.assertTrue(self.logged("fragments into"))
        rows = read(a, "int32", [(4, (ANY, ANY), [1, 2])], [a.pid])[a.pid]
        self.assertEqual(len(rows), 240)
        self.assertEqual(rows[0], (4, (0, 0), {1: 0, 2: 1}))
        self.assertEqual(rows[-1], (4, (5, 39), {1: 39 * 3 + 5, 2: 1}))
        self.assertEqual(sorted(r[1] for r in rows), [(c, i) for c in range(6) for i in range(40)])
        self.assertFalse(a.secure.stuck())

    def test_a_send_with_no_groups_is_answered(self):
        a = self.player("alice")
        self.assertEqual(send(a, "int32"), {})
        self.assertEqual(read(a, "int32", [(1, (), [1])], [a.pid]), {a.pid: []})


class Keeping(unittest.TestCase):
    def setUp(self):
        self.data_dir = tempfile.mkdtemp(prefix="grfs-test-")
        self.addCleanup(shutil.rmtree, self.data_dir, True)

    def test_every_type_survives_a_restart(self):
        with Harness(data_dir=self.data_dir) as server:
            alice = server.player("alice")
            bob = server.player("bob")
            send(alice, "int32", (1, (), [(1, 1, SET)]), (2, (3,), [(1, 4500, SET)]),
                 (0xd, (1,), [(5, 12, ADD)]))
            send(alice, "int64", (2, (3,), [(7, 1 << 45, SET)]))
            send(alice, "float", (9, (1,), [(1, 0.75, SET), (2, math.inf, SET)]))
            send(alice, "string", (0x14, (1,), [(1, "Ghost Lead \xe9", SET)]))
            send(alice, "blob", (3, (0, 1), [(9, b"\x00\xff\x10", SET)]))
            send(bob, "int32", (1, (), [(1, 2, SET)]))
            pids = alice.pid, bob.pid
            server.assert_clean()
        path = os.path.join(self.data_dir, "stats.json")
        with open(path, encoding="utf-8") as f:
            json.load(f)                 # plain JSON, an infinite float included

        with Harness(data_dir=self.data_dir) as server:
            alice = server.player("alice")
            self.assertEqual(alice.pid, pids[0])
            self.assertEqual(read(alice, "int32", [(1, (), [1]), (2, (ANY,), [1]),
                                                   (0xd, (1,), [5])], pids),
                             {pids[0]: [(1, (), {1: 1}), (2, (3,), {1: 4500}),
                                        (0xd, (1,), {5: 12})],
                              pids[1]: [(1, (), {1: 2})]})
            self.assertEqual(value(alice, "int64", 2, (3,), 7), 1 << 45)
            self.assertEqual(read(alice, "float", [(9, (1,), [1, 2])], [alice.pid]),
                             {alice.pid: [(9, (1,), {1: 0.75, 2: math.inf})]})
            self.assertEqual(value(alice, "string", 0x14, (1,), 1), "Ghost Lead \xe9")
            self.assertEqual(value(alice, "blob", 3, (0, 1), 9), b"\x00\xff\x10")
            # The career goes on adding to what was kept.
            send(alice, "int32", (0xd, (1,), [(5, 3, ADD)]))
            self.assertEqual(value(alice, "int32", 0xd, (1,), 5), 15)
            server.assert_clean()

        with Harness(data_dir=self.data_dir) as server:
            alice = server.player("alice")
            self.assertEqual(value(alice, "int32", 0xd, (1,), 5), 15)
            server.assert_clean()

    def test_each_send_is_on_disk_before_the_server_stops(self):
        """Progress must not wait for a clean shutdown: a crash after a match keeps the match."""
        with Harness(data_dir=self.data_dir) as server:
            alice = server.player("alice")
            send(alice, "int32", (2, (0,), [(1, 300, SET)]))
            with open(os.path.join(self.data_dir, "stats.json"), encoding="utf-8") as f:
                rows = json.load(f)["players"][str(alice.pid)]
            self.assertEqual(rows, [{"kind": "int32", "board": 2, "contexts": [0],
                                     "stats": [[1, 300]]}])
            server.assert_clean()

    def test_a_damaged_file_is_set_aside_not_overwritten(self):
        path = os.path.join(self.data_dir, "stats.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write('{"players": {"4096": [{"kind": "int32", "board"')
        with Harness(data_dir=self.data_dir) as server:
            self.assertTrue(server.capture.has("cannot read"))
            alice = server.player("alice")
            self.assertEqual(read(alice, "int32", [(1, (), [1])], [alice.pid]), {alice.pid: []})
            send(alice, "int32", (1, (), [(1, 1, SET)]))
        aside = [n for n in os.listdir(self.data_dir) if n.startswith("stats.json.bad-")]
        self.assertEqual(len(aside), 1)
        with open(os.path.join(self.data_dir, aside[0]), encoding="utf-8") as f:
            self.assertIn('"board"', f.read())

    def test_unreadable_rows_are_left_out_and_the_rest_kept(self):
        path = os.path.join(self.data_dir, "stats.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"players": {"4096": [
                {"kind": "int32", "board": 1, "contexts": [], "stats": [[1, 6]]},
                {"kind": "nonsense", "board": 1, "contexts": [], "stats": [[1, 6]]},
                {"kind": "blob", "board": 1, "contexts": [], "stats": [[1, "not base64!"]]},
                {"kind": "blob", "board": 1, "contexts": [], "stats": [[1, 5]]},
                {"kind": "string", "board": 1, "contexts": [], "stats": {"12": "x"}},
                {"kind": "int32", "board": 0x101, "contexts": [], "stats": [[1, 9]]},
                {"kind": "int32", "board": 1, "contexts": [1 << 32], "stats": [[1, 9]]},
                {"kind": "int32", "board": 1, "contexts": [], "stats": [[0x101, 9]]},
                {"kind": "int32", "board": 1, "contexts": [], "stats": [[2, "7"]]},
                {"kind": "int32", "board": 2}, "a row that is no row"],
                "0": [{"kind": "int32", "board": 1, "contexts": [], "stats": [[1, 1]]}],
                "4097": {"kind": "int32"}}}, f)
        with self.assertLogs("grfs.stats", "WARNING") as logged:
            store = model.Stats(path)
        self.assertIn("12 unreadable row(s)", "\n".join(logged.output))
        self.assertEqual(store.get("int32", 4096, 1, (), 1), 6)
        self.assertEqual(store.get("int32", 4096, 1, ()), {1: 6})
        self.assertEqual(store.boards(4096), {"int32": 1})
        self.assertEqual(len(store), 1)

    def test_a_server_starts_whatever_rows_it_cannot_read(self):
        """One hand-edited value must not keep everyone else's profile off line."""
        path = os.path.join(self.data_dir, "stats.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"players": {"4096": [
                {"kind": "blob", "board": 1, "contexts": [], "stats": [[1, 5]]}]}}, f)
        with Harness(data_dir=self.data_dir) as server:
            self.assertTrue(server.capture.has("unreadable row"))
            alice = server.player("alice")
            send(alice, "int32", (1, (), [(1, 1, SET)]))
            self.assertEqual(value(alice, "int32", 1, (), 1), 1)
            server.assert_clean()

    def test_a_server_without_a_data_directory_keeps_nothing(self):
        with Harness(data_dir="") as server:
            alice = server.player("alice")
            send(alice, "int32", (1, (), [(1, 1, SET)]))
            self.assertEqual(value(alice, "int32", 1, (), 1), 1)
            service = server.run(lambda: server.backend.router.service_for(0x6C))
            self.assertIsNone(service.stats.path)
            server.assert_clean()


if __name__ == "__main__":
    unittest.main()
