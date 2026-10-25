"""MiscProtocol and MapListProtocol against a running server: what a matchmade host needs before
its lobby exists - a map for its mode, then a match key - and the map table behind the maps."""

import importlib.util
import io
import json
import os
import shutil
import struct
import tempfile
import time
import unittest
from unittest import mock

from grfs import fakegame
from grfs import maps as model
from grfs.harness import Harness
from grfs.rmc.router import Errors
from grfs.services.misc import pack_date

from tests.live import LiveTest

M = "MiscProtocol"
L = "MapListProtocol"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GAME = os.environ.get("GRFS_GAME_DIR", r"D:\SteamLibrary\steamapps\common\Tom Clancy's Ghost "
                                       r"Recon Future Soldier")

CONFLICT, DECOY, SABOTEUR, SIEGE, STOCKPILE, MODE6 = 0x10, 0x20, 0x40, 0x80, 0x100, 0x200
MODES = (CONFLICT, DECOY, SABOTEUR, SIEGE, STOCKPILE, MODE6)
BASE, DLC1, DLC2 = model.BASE, model.DLC1, model.DLC2


def load_tool(name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, "tools", name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def table():
    with open(model.PATH, encoding="utf-8") as f:
        return json.load(f)


def worlds(mode_bit=None, name_ids=None) -> set:
    """Every adversarial world key in schema/maps.json, of one mode and some maps if asked."""
    inverse = {bit: mode for mode, bit in model.MODE_BITS.items()}
    out = set()
    for row in table()["maps"]:
        if row["type"] != model.ADVERSARIAL:
            continue
        if name_ids is not None and row["name_id"] not in name_ids:
            continue
        for mode, keys in row["modes"].items():
            if mode_bit is None or int(mode) == inverse[mode_bit]:
                out.update(int(k, 16) for k in keys)
    return out


def u32_list(body: bytes) -> list:
    count, = struct.unpack_from("<I", body)
    assert len(body) == 4 + 4 * count, body.hex(" ")
    return list(struct.unpack_from("<%dI" % count, body, 4))


# ----- Misc ---------------------------------------------------------------------------------
class MatchKey(LiveTest):
    def test_every_call_is_a_new_match(self):
        alice, bob = self.player("alice"), self.player("bob")
        keys = [alice.call(M, "GetMatchUniqueKey")["uniqueKey"] for _ in range(5)]
        keys.append(bob.call(M, "GetMatchUniqueKey")["uniqueKey"])
        keys.append(alice.call(M, "GetMatchUniqueKey")["uniqueKey"])
        self.assertTrue(all(keys), keys)
        self.assertEqual(len(set(keys)), len(keys))
        self.assertEqual(keys, sorted(keys))
        # The telemetry prints it with %llu and every peer stores it: a plain 64-bit number,
        # under the sign bit.
        self.assertTrue(all(0 < k < 1 << 63 for k in keys))
        self.assertTrue(self.logged("alice hosts match %d" % keys[0]))

    def test_the_reply_is_eight_bytes(self):
        reply = self.player("alice").call_raw(M, 1)
        self.assertTrue(reply.success)
        self.assertEqual(len(reply.body), 8)
        self.assertNotEqual(struct.unpack("<Q", reply.body)[0], 0)


class MatchKeyAcrossRestarts(unittest.TestCase):
    def setUp(self):
        self.data_dir = tempfile.mkdtemp(prefix="grfs-test-")
        self.addCleanup(shutil.rmtree, self.data_dir, True)

    def key(self, server):
        return server.player("alice").call(M, "GetMatchUniqueKey")["uniqueKey"]

    def test_a_restart_never_repeats_a_key(self):
        with Harness(data_dir=self.data_dir) as server:
            first = self.key(server)
            server.assert_clean()
        with open(os.path.join(self.data_dir, "match_keys.json"), encoding="utf-8") as f:
            self.assertEqual(json.load(f), {"last": first})
        with Harness(data_dir=self.data_dir) as server:
            self.assertGreater(self.key(server), first)
            server.assert_clean()

    def test_a_clock_set_back_does_not_repeat_one_either(self):
        # A key from a server whose clock was a day ahead.
        ahead = (int(time.time() * 1000) + 86400 * 1000) << 12
        with open(os.path.join(self.data_dir, "match_keys.json"), "w", encoding="utf-8") as f:
            json.dump({"last": ahead}, f)
        with Harness(data_dir=self.data_dir) as server:
            self.assertEqual(self.key(server), ahead + 1)
            server.assert_clean()

    def test_an_unreadable_file_is_an_error_but_keys_still_come(self):
        with open(os.path.join(self.data_dir, "match_keys.json"), "w", encoding="utf-8") as f:
            f.write("{not json")
        with Harness(data_dir=self.data_dir) as server:
            self.assertGreater(self.key(server), 0)
            self.assertTrue(server.capture.has("match keys start from the clock"))


class GenericSettings(LiveTest):
    def test_by_default_only_the_clients_own_default(self):
        alice = self.player("alice")
        self.assertEqual(alice.call(M, "GetGenericSettings"),
                         {"settings": [{"key": 100, "value": 3}]})
        reply = alice.call_raw(M, 3)
        self.assertEqual(reply.body, struct.pack("<3I", 1, 100, 3))


class GenericSettingsFromTheConfig(LiveTest):
    settings = {"generic_settings": "2=20, 0=2026-12-24, 1=2026-12-27 06:30, 100=0x8, x=1, 5=-1"}

    def test_the_config_adds_and_overrides(self):
        answer = self.player("alice").call(M, "GetGenericSettings")["settings"]
        self.assertEqual(answer, [{"key": 0, "value": pack_date(2026, 12, 24)},
                                  {"key": 1, "value": pack_date(2026, 12, 27, 6, 30)},
                                  {"key": 2, "value": 20},
                                  {"key": 100, "value": 8}])
        self.assertTrue(self.logged("ignoring 'x=1'"))
        self.assertTrue(self.logged("ignoring '5=-1'"))

    def test_a_second_player_gets_the_same(self):
        a = self.player("alice").call(M, "GetGenericSettings")
        b = self.player("bob").call(M, "GetGenericSettings")
        self.assertEqual(a, b)


class PackedDates(unittest.TestCase):
    def test_the_layout_the_client_unpacks(self):
        v = pack_date(2026, 12, 27, 6, 30)
        # A68C40: tm_year = (v & 0x7ff) - 900, tm_mon = (v >> 11 & 0xf) - 1, tm_mday = v >> 15
        # & 0x1f, tm_hour = v >> 20 & 0x1f, tm_min = v >> 25 & 0x3f.
        self.assertEqual((v & 0x7FF) - 900 + 1900, 2026)
        self.assertEqual(v >> 11 & 0xF, 12)
        self.assertEqual(v >> 15 & 0x1F, 27)
        self.assertEqual(v >> 20 & 0x1F, 6)
        self.assertEqual(v >> 25 & 0x3F, 30)
        self.assertRaises(ValueError, pack_date, 2026, 13, 1)
        self.assertRaises(ValueError, pack_date, 999, 1, 1)
        self.assertRaises(ValueError, pack_date, 2026, 2, 30)
        self.assertRaises(ValueError, pack_date, 2026, 1, 1, 24, 0)


class PrivilegeTrials(LiveTest):
    def test_both_methods_succeed_with_nothing(self):
        alice = self.player("alice")
        self.assertEqual(alice.call(M, "UnlockPrivilegeTrial", {"privilegeId": 0x13CE}), {})
        self.assertEqual(alice.call(M, "UnlockPrivilegeTrial2", {"privilegeId": 3100}), {})
        reply = alice.call_raw(M, 4, struct.pack("<I", 0x13D6))
        self.assertTrue(reply.success)
        self.assertEqual(reply.body, b"")
        self.assertTrue(self.logged("alice unlocks a trial of privilege 5070"))

    def test_the_dead_method_is_an_error_not_silence(self):
        reply = self.player("alice").call_raw(M, 2, b"")
        self.assertFalse(reply.success)
        self.assertEqual(reply.error, Errors.CORE_INVALID_ARGUMENT)


# ----- MapList ------------------------------------------------------------------------------
class Categories(LiveTest):
    def test_one_category_per_mode_with_every_bit_the_client_knows(self):
        alice = self.player("alice")
        categories = alice.call(L, "GetAvailableMapCategories")["categories"]
        self.assertEqual([c & model.ANY_MODE for c in categories], list(MODES))
        for c in categories:
            self.assertEqual(c & ~model.ANY_MODE, 0x1 | 0x2 | BASE | DLC1 | DLC2)
        self.assertEqual(u32_list(alice.call_raw(L, 1).body), categories)
        self.assertEqual(self.player("bob").call(L, "GetAvailableMapCategories")["categories"],
                         categories)


class MapLists(LiveTest):
    def setUp(self):
        super().setUp()
        self.alice = self.player("alice")

    def maps(self, mask):
        return self.alice.call(L, "GetMapList", {"categoryId": mask})["mapIds"]

    def test_every_mode_has_maps_and_only_its_own(self):
        for bit in MODES:
            for flags in (0, BASE, BASE | 0x1, BASE | 0x2):
                keys = self.maps(bit | flags)
                self.assertEqual(len(keys), 10, hex(bit | flags))
                self.assertEqual(len(set(keys)), 10)
                self.assertTrue(set(keys) <= worlds(bit, model.POOLS[BASE]), hex(bit | flags))

    def test_the_known_keys_are_there(self):
        self.assertIn(0xA9211863, self.maps(CONFLICT | BASE))      # Pipeline (MA01) Conflict
        self.assertIn(0x222A8978, self.maps(CONFLICT | BASE))      # Market (MA02) Conflict
        self.assertIn(0x222AC79A, self.maps(STOCKPILE | BASE))     # MA01 _gSTO, config-dlc1.xml
        self.assertIn(0x3220F4DE, self.maps(CONFLICT | DLC1))      # MAD01 _gCON
        self.assertIn(0x3220FC58, self.maps(CONFLICT | DLC1))      # MAD03 _gCON

    def test_dlc_pools(self):
        self.assertEqual(set(self.maps(CONFLICT | DLC1)),
                         worlds(CONFLICT, model.POOLS[DLC1]))
        self.assertEqual(len(self.maps(DECOY | DLC1)), 3)
        self.assertEqual(set(self.maps(SIEGE | DLC2)), worlds(SIEGE, model.POOLS[DLC2]))
        both = self.maps(SABOTEUR | BASE | DLC1 | DLC2)
        self.assertEqual(len(both), 16)
        self.assertEqual(set(both), worlds(SABOTEUR, model.POOLS[BASE] + model.POOLS[DLC1]
                                           + model.POOLS[DLC2]))

    def test_a_combined_mask_is_every_mode_it_names(self):
        keys = self.maps(CONFLICT | SIEGE | BASE)
        self.assertEqual(set(keys), worlds(CONFLICT, model.POOLS[BASE])
                         | worlds(SIEGE, model.POOLS[BASE]))
        self.assertEqual(len(self.maps(model.ANY_MODE | BASE)), 60)

    def test_never_empty(self):
        # Nothing named at all, only flags, a mode no pool has, bits nobody uses.
        for mask in (0, 0x1, 0x2, BASE, DLC2, 0x8, 0x400, 0xFFFFFFFF, 0x80000000):
            keys = self.maps(mask)
            self.assertTrue(keys, hex(mask))
            self.assertTrue(set(keys) <= worlds(), hex(mask))

    def test_no_map_outside_the_pools(self):
        night = {0x1E90C, 0x1E90D}
        self.assertFalse(set(self.maps(0xFFFFFFFF)) & worlds(name_ids=night))

    def test_the_hosts_two_requests(self):
        """As 104738E0 and 10473A80 do it: the first request with 0x4000 cleared unless it is
        the only pool or both DLCs are in, a random pick, the same request again with the raw
        mask - and the pick must be in the second answer."""
        for raw in (CONFLICT | 0x1 | BASE, DECOY | 0x2 | BASE | DLC1,
                    SIEGE | 0x1 | BASE | DLC1 | DLC2, MODE6 | 0x2 | BASE | DLC2):
            first = raw
            if raw & 0x18000 != 0x18000 and raw & 0x1C000 != 0x4000:
                first &= ~BASE
            picks = self.maps(first)
            self.assertTrue(picks, hex(first))
            second = self.maps(raw)
            self.assertTrue(set(picks) <= set(second), hex(raw))
            self.assertEqual(self.maps(raw), second)            # the same answer every time

    def test_the_wire_layout(self):
        reply = self.alice.call_raw(L, 2, struct.pack("<I", CONFLICT | BASE))
        self.assertTrue(reply.success)
        self.assertEqual(u32_list(reply.body), self.maps(CONFLICT | BASE))

    def test_a_second_player_sees_the_same(self):
        bob = self.player("bob")
        self.assertEqual(bob.call(L, "GetMapList", {"categoryId": DECOY | BASE}),
                         {"mapIds": self.maps(DECOY | BASE)})


class WithoutTheMapTable(LiveTest):
    """A server whose schema/maps.json is gone still starts and serves everything else; only
    GetMapList fails, with an error the host turns into "back to the start" instead of a key it
    cannot load."""

    expect_errors = True

    def setUp(self):
        patch = mock.patch.object(model, "Maps", side_effect=model.MapError("no table here"))
        patch.start()
        self.addCleanup(patch.stop)
        super().setUp()

    def test_the_map_list_is_an_error_and_the_rest_works(self):
        self.assertTrue(self.logged("no table here - GetMapList will fail"))
        alice = self.player("alice")
        with self.assertRaises(fakegame.RmcFailure) as failure:
            alice.call(L, "GetMapList", {"categoryId": CONFLICT | BASE})
        self.assertEqual(failure.exception.code, Errors.CORE_UNKNOWN)
        self.assertEqual(len(alice.call(L, "GetAvailableMapCategories")["categories"]), 6)
        self.assertGreater(alice.call(M, "GetMatchUniqueKey")["uniqueKey"], 0)


class TheHostingStep(LiveTest):
    def test_map_then_key(self):
        """The order the lobby-host step asks in, against one server."""
        host = self.player("host")
        categories = host.call(L, "GetAvailableMapCategories")["categories"]
        mask = (categories[0] & (model.ANY_MODE | BASE)) | 0x1
        first = host.call(L, "GetMapList", {"categoryId": mask})["mapIds"]
        second = host.call(L, "GetMapList", {"categoryId": mask})["mapIds"]
        self.assertEqual(first, second)
        key = host.call(M, "GetMatchUniqueKey")["uniqueKey"]
        self.assertGreater(key, 0)
        self.assertTrue(self.logged("host may host 10 world(s) for mask 0x4011"))


# ----- the map table ------------------------------------------------------------------------
class TheMapTable(unittest.TestCase):
    """schema/maps.json against what the notes found independently."""

    def test_config_dlc1_keys_and_their_modes(self):
        # config-dlc1.xml: MA01..MA10 _gSTO, then MAD01..03 _gCON/_gDEC/_gSAF/_gSIG/_gSTO.
        sto = [0x222AC79A, 0x222AC991, 0x222AC0D6, 0x222ACB1F, 0x222ACB44, 0x222ACB69,
               0x222ACB7B, 0x222ACB8D, 0x222AC535, 0x222ACBA6]
        self.assertEqual([m.keys[STOCKPILE] for m in model.Maps().pool(BASE)], sto)
        mad = {0x1E670: (0x3220F4DE, 0x3220F542, 0x3220F55F, 0x3220F67E, 0xA0333E99),
               0x1E672: (0x3220F9DB, 0x3220FA0F, 0x3220FA2C, 0x3220FA72, 0xBC3291BC),
               0x1E673: (0x3220FC58, 0x3220FCB5, 0x3220FCE5, 0x3220FD02, 0x3220EFA1)}
        maps = model.Maps()
        for name_id, keys in mad.items():
            m = maps.by_name_id[name_id]
            self.assertEqual((m.keys[CONFLICT], m.keys[DECOY], m.keys[SABOTEUR], m.keys[SIEGE],
                              m.keys[STOCKPILE]), keys, hex(name_id))

    def test_the_rest_of_the_notes(self):
        maps = model.Maps()
        self.assertEqual(maps.by_name_id[0x1A0E6].keys[CONFLICT], 0xA9211863)
        self.assertEqual(maps.by_name_id[0x1A0E7].keys[CONFLICT], 0x222A8978)
        self.assertEqual(maps.by_name_id[0x1E676].keys[MODE6], 0xAB314A46)
        self.assertEqual(maps.by_name_id[0x1A0E6].name, "Pipeline")
        records = {int(r["key"], 16): r for r in table()["records"]}
        arctic = records[0x9F2E1B5B]                              # Arctic Base Guerilla
        self.assertEqual((arctic["type"], arctic["mode"], arctic["name_id"]), (1, 8, 0x1E679))
        self.assertEqual(len(table()["records"]), 236)

    def test_every_pool_map_has_all_six_modes(self):
        for pool in model.POOLS:
            for m in model.Maps().pool(pool):
                self.assertEqual(sorted(m.keys), sorted(MODES), m)

    def test_a_missing_table_is_an_error(self):
        self.assertRaises(model.MapError, model.Maps, os.path.join(ROOT, "no-such-maps.json"))


class Extractor(unittest.TestCase):
    """tools/extract_maps.py on a table built here, in the bigfile's own format."""

    FIELDS = [("UniqueID", 0, 0), ("World key", 9, 4), ("Name id", 0, 8), ("Junk", 1, 0xC),
              ("Type", 5, 0x10), ("GameMode", 5, 0x14), ("Late", 0, 0)]
    STRIDE = 0x24

    def blob(self, records, garbage=b"\xff" * 64):
        out = bytearray(b"\x00" * 37)                        # whatever comes before
        out += struct.pack("<3I", 0x18, 0x1234, len(self.FIELDS))
        for name, ftype, offset in self.FIELDS:
            out += name.encode() + b"\x00" + bytes([ftype]) + struct.pack("<I", offset)
        start = len(out)
        for uid, key, name_id, typ, mode in records:
            rec = struct.pack("<6I", uid, key, name_id, 0x3F800000, typ, mode)
            out += rec + b"\xab" * (self.STRIDE - len(rec))
        return bytes(out + garbage), start

    RECORDS = [(10, 0x11111111, 0x1A0E6, 2, 2), (11, 0x22222222, 0x1A0E6, 2, 3),
               (13, 0x33333333, 0x1E679, 1, 8), (20, 0x44444444, 0xFFFFFFFF, 0, 0),
               (21, 0x55555555, 0x1A0E6, 2, 2)]

    def test_the_parser(self):
        tool = load_tool("extract_maps")
        data, start = self.blob(self.RECORDS)
        found = tool.find_tables(data)
        self.assertEqual(len(found), 1)
        parsed = tool.parse_table(data, found[0])
        self.assertEqual(parsed["offset"], start)
        self.assertEqual(parsed["stride"], self.STRIDE)
        self.assertEqual([(r["UniqueID"], r["World key"], r["Name id"], r["Type"], r["GameMode"])
                          for r in parsed["records"]], self.RECORDS)
        self.assertEqual(parsed["fields"][1], ("World key", 9, 4))

        built = tool.build(parsed, {0x1A0E6: "Pipeline"}, "test.big")
        self.assertEqual(len(built["records"]), 5)
        pipeline = [m for m in built["maps"] if m["name_id"] == 0x1A0E6]
        self.assertEqual(pipeline, [{"name_id": 0x1A0E6, "name": "Pipeline", "type": 2,
                                     "modes": {"2": ["0x11111111", "0x55555555"],
                                               "3": ["0x22222222"]}}])
        self.assertFalse([m for m in built["maps"] if m["name_id"] == 0xFFFFFFFF])

    def test_the_table_ends_at_the_first_bad_record(self):
        tool = load_tool("extract_maps")
        # A unique id going backwards is the end, and so is a record that is not one.
        data, _ = self.blob(self.RECORDS[:3] + [(5, 1, 2, 2, 2)] + self.RECORDS[3:])
        self.assertEqual(len(tool.parse_table(data, tool.find_tables(data)[0])["records"]), 3)
        data, _ = self.blob(self.RECORDS[:2] + [(30, 1, 2, 77, 2)])
        self.assertEqual(len(tool.parse_table(data, tool.find_tables(data)[0])["records"]), 2)

    def test_a_table_without_the_fields_is_refused(self):
        tool = load_tool("extract_maps")
        self.FIELDS = [f for f in Extractor.FIELDS if f[0] != "GameMode"]
        data, _ = self.blob(self.RECORDS)
        self.assertRaises(tool.FormatError, tool.parse_table, data, tool.find_tables(data)[0])

    def test_the_command_line(self):
        tool = load_tool("extract_maps")
        work = tempfile.mkdtemp(prefix="grfs-test-")
        self.addCleanup(shutil.rmtree, work, True)
        data, _ = self.blob(self.RECORDS)
        with open(os.path.join(work, "Yeti.big"), "wb") as f:
            f.write(data)
        os.makedirs(os.path.join(work, "Language"))
        with open(os.path.join(work, "Language", "Oasis_English.inf"), "wb") as f:
            f.write(b"Version=3\n// ID:Type:Mask:{custom:}TextUTF8\n106726:1:0:Pipeline\n"
                    b"124537:1:1:868:96470:Arctic Base\n")
        out = os.path.join(work, "maps.json")
        stdout = io.StringIO()
        from contextlib import redirect_stdout
        with redirect_stdout(stdout):
            self.assertEqual(tool.main([os.path.join(work, "Yeti.big"), "-o", out]), 0)
        with open(out, encoding="utf-8") as f:
            written = json.load(f)
        self.assertEqual({m["name_id"]: m["name"] for m in written["maps"]},
                         {0x1A0E6: "Pipeline", 0x1E679: "Arctic Base"})
        self.assertIn("5 records", stdout.getvalue())

        with open(os.path.join(work, "Empty.big"), "wb") as f:
            f.write(b"\x00" * 100)
        from contextlib import redirect_stderr
        with redirect_stderr(io.StringIO()):
            self.assertEqual(tool.main([os.path.join(work, "Empty.big"), "-o", out]), 1)

    @unittest.skipUnless(os.path.exists(os.path.join(GAME, "Yeti.big")), "the game is not here")
    def test_the_game_gives_the_table_in_the_tree(self):
        tool = load_tool("extract_maps")
        with open(model.PATH, encoding="utf-8") as f:
            in_tree = f.read()
        self.assertEqual(tool.dumps(tool.extract(os.path.join(GAME, "Yeti.big"))), in_tree)


if __name__ == "__main__":
    unittest.main()
