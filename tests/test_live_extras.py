"""The small protocols the game calls after sign-in, against a running server: Uplay's actions and
rewards, the game's awards, telemetry, the locale, title storage and key activation. None of them
may answer with an error the client does not expect, and what is kept per player stays that
player's, and stays across a restart."""

import json
import os
import shutil
import tempfile
import unittest

from grfs.fakegame import RmcFailure
from grfs.harness import Harness
from grfs.kerberos import TRACKING_PID
from grfs.rmc.stream import Reader, Writer

from tests.live import LiveTest

U = "UplayWinProtocol"
A = "GR5AwardProtocol"
T = "TrackingProtocol3"
P = "PrivilegesProtocol"

REWARD_CODES = ["GRFSREWARD01PC", "GRFSREWARD02", "GRFSREWARD03", "GRFSREWARD04"]
REWARD_PRIVILEGES = [0x1432, 0x143C, 0x1446, 0x1450]
EVERYTHING = {"startRowIndex": 0, "maximumRows": 100, "sortExpression": "GRFS",
              "cultureName": "en-US", "platformCode": "PC"}
WHERE = {"cultureName": "en-US", "platformCode": "PC"}


def completed(actions) -> dict:
    """code -> whether the PC entry says it is done."""
    return {a["m_code"]: any(p["m_platformCode"] == "PC" and p["m_completed"]
                             for p in a["m_platforms"]) for a in actions}


def award_ids(reply) -> list:
    return [a["m_awardId"] for a in reply["awards"]]


class Uplay(LiveTest):
    def test_the_sign_in_sequence_is_answered_in_full(self):
        alice = self.player("alice")
        self.assertEqual(alice.call(U, "UplayWelcome", WHERE), {"actions": []})
        self.assertEqual(alice.call(U, "SetActionsCompleted", dict(WHERE, actionCodeList=[])),
                         {"actions": []})
        rewards = alice.call(U, "GetRewards", EVERYTHING)["rewards"]
        actions = alice.call(U, "GetActions", EVERYTHING)["actions"]
        self.assertTrue(self.wait_logged("UplayWinProtocol.GetActions"))
        self.assertFalse(self.logged("not implemented"))

        # Every reward the client knows, bought on PC - which is what makes it unlock them.
        self.assertEqual([r["m_code"] for r in rewards], REWARD_CODES)
        for r in rewards:
            self.assertIn({"m_platformCode": "PC", "m_purchased": True}, r["m_platforms"])
        self.assertEqual([r["m_value"] for r in rewards], [10, 20, 30, 40])
        # The four actions the game completes, none of them done yet.
        self.assertEqual(completed(actions), {"1": False, "2": False, "3": False, "4": False})
        self.assertEqual([a["m_value"] for a in actions], [10, 20, 30, 40])

    def test_what_the_rewards_unlock_is_already_owned(self):
        # Otherwise the client asks Misc UnlockPrivilegeTrial for each of them on every sign-in.
        owned = {p["m_ID"] for p in self.player("alice").call(P, "GetPrivilegesEx",
                                                              {"localeCode": "en-US"})
                 ["privilegesEx"]}
        self.assertLessEqual(set(REWARD_PRIVILEGES), owned)

    def test_rewards_come_a_page_at_a_time(self):
        alice = self.player("alice")
        page = alice.call(U, "GetRewards", dict(EVERYTHING, startRowIndex=1, maximumRows=2))
        self.assertEqual([r["m_code"] for r in page["rewards"]], REWARD_CODES[1:3])
        past = alice.call(U, "GetRewards", dict(EVERYTHING, startRowIndex=10))
        self.assertEqual(past["rewards"], [])

    def test_a_completed_action_is_that_players_alone(self):
        alice, bob = self.player("alice"), self.player("bob")
        action = alice.call(U, "SetActionCompleted", dict(WHERE, actionCode="2"))["action"]
        self.assertEqual(action["m_code"], "2")
        self.assertEqual(action["m_value"], 20)
        self.assertTrue(completed([action])["2"])
        self.assertTrue(action["m_name"])           # the client shows it

        self.assertEqual(completed(alice.call(U, "GetActions", EVERYTHING)["actions"]),
                         {"1": False, "2": True, "3": False, "4": False})
        self.assertEqual([a["m_code"] for a in alice.call(U, "UplayWelcome", WHERE)["actions"]],
                         ["2"])
        self.assertFalse(any(completed(bob.call(U, "GetActions", EVERYTHING)["actions"])
                             .values()))
        self.assertEqual(bob.call(U, "UplayWelcome", WHERE), {"actions": []})

    def test_completing_twice_is_no_different_from_once(self):
        alice = self.player("alice")
        for _ in range(2):
            alice.call(U, "SetActionCompleted", dict(WHERE, actionCode="1"))
        self.assertEqual(self.count_logged("completed Uplay action(s) 1"), 1)
        self.assertEqual(self.on_server(
            lambda: self.server.backend.router.services[0x78].completed.get(alice.pid)), ["1"])

    def test_an_action_nobody_knows_did_not_happen(self):
        alice = self.player("alice")
        action = alice.call(U, "SetActionCompleted", dict(WHERE, actionCode="77"))["action"]
        # -1 is what the client reads as "it failed".
        self.assertEqual(action["m_value"], 0xFFFFFFFF)
        self.assertEqual(action["m_code"], "77")
        self.assertFalse(any(completed(alice.call(U, "GetActions", EVERYTHING)["actions"])
                             .values()))

    def test_actions_done_offline_are_taken_and_only_known_ones_answered(self):
        alice = self.player("alice")
        reply = alice.call(U, "SetActionsCompleted", dict(WHERE, actionCodeList=["4", "x", "1"]))
        self.assertEqual(completed(reply["actions"]), {"1": True, "4": True})
        self.assertEqual(completed(alice.call(U, "GetActions", EVERYTHING)["actions"]),
                         {"1": True, "2": False, "3": False, "4": True})


class Awards(LiveTest):
    RANGE = {"m_uiOffset": 0, "m_uiSize": 150}

    def test_granted_awards_read_back(self):
        alice = self.player("alice")
        self.assertEqual(alice.call(A, "getGrantedAwards", {"pid": alice.pid,
                                                             "resultRange": self.RANGE}),
                         {"awards": []})
        self.assertEqual(alice.call(A, "grantAwards", {"awardIds": [5, 7]}), {})
        reply = alice.call(A, "getGrantedAwards", {"pid": alice.pid, "resultRange": self.RANGE})
        self.assertEqual(reply["awards"], [{"m_awardId": 5, "m_param": 0},
                                           {"m_awardId": 7, "m_param": 0}])
        self.assertEqual(alice.call(A, "getGrantedAwardsCount", {"pid": alice.pid}),
                         {"count": 2})

    def test_an_award_is_granted_once(self):
        alice = self.player("alice")
        alice.call(A, "grantAwards", {"awardIds": [5]})
        alice.call(A, "grantAwards", {"awardIds": [5, 9]})
        self.assertEqual(award_ids(alice.call(A, "getGrantedAwards",
                                              {"pid": alice.pid, "resultRange": self.RANGE})),
                         [5, 9])

    def test_another_player_sees_whose_they_ask_for(self):
        alice, bob = self.player("alice"), self.player("bob")
        alice.call(A, "grantAwards", {"awardIds": [3]})
        self.assertEqual(award_ids(bob.call(A, "getGrantedAwards",
                                            {"pid": alice.pid, "resultRange": self.RANGE})), [3])
        self.assertEqual(award_ids(bob.call(A, "getGrantedAwards",
                                            {"pid": bob.pid, "resultRange": self.RANGE})), [])
        self.assertEqual(bob.call(A, "getGrantedAwardsCount", {"pid": alice.pid}), {"count": 1})

    def test_a_pid_nobody_has_answers_for_the_caller(self):
        alice = self.player("alice")
        alice.call(A, "grantAwards", {"awardIds": [11]})
        self.assertEqual(award_ids(alice.call(A, "getGrantedAwards",
                                              {"pid": 0xDEAD, "resultRange": self.RANGE})), [11])
        self.assertEqual(award_ids(alice.call(A, "getGrantedAwards",
                                              {"pid": 0, "resultRange": self.RANGE})), [11])

    def test_an_award_sent_twice_in_one_grant_counts_once(self):
        alice = self.player("alice")
        alice.call(A, "grantAwards", {"awardIds": [6, 6, 8]})
        alice.call(A, "grantAwards", {"awardIds": [8, 8]})
        self.assertEqual(award_ids(alice.call(A, "getGrantedAwards",
                                              {"pid": alice.pid, "resultRange": self.RANGE})),
                         [6, 8])
        self.assertEqual(alice.call(A, "getGrantedAwardsCount", {"pid": alice.pid}),
                         {"count": 2})
        self.assertTrue(self.wait_logged("earned award(s) - (1 already granted)"))

    def test_the_telemetry_user_is_nobody_whose_awards_are_asked_for(self):
        alice = self.player("alice")
        alice.call(A, "grantAwards", {"awardIds": [4]})
        self.assertEqual(award_ids(alice.call(A, "getGrantedAwards",
                                              {"pid": TRACKING_PID, "resultRange": self.RANGE})),
                         [4])

    def test_the_range_is_honoured(self):
        alice = self.player("alice")
        alice.call(A, "grantAwards", {"awardIds": [1, 2, 3, 4]})
        self.assertEqual(award_ids(alice.call(A, "getGrantedAwards", {
            "pid": alice.pid, "resultRange": {"m_uiOffset": 1, "m_uiSize": 2}})), [2, 3])


class KeptAcrossRestarts(unittest.TestCase):
    def setUp(self):
        self.data_dir = tempfile.mkdtemp(prefix="grfs-test-")
        self.addCleanup(shutil.rmtree, self.data_dir, True)

    def read(self, name):
        with open(os.path.join(self.data_dir, name), encoding="utf-8") as f:
            return json.load(f)

    def test_awards_and_actions_survive_a_restart(self):
        with Harness(data_dir=self.data_dir) as server:
            alice = server.player("alice")
            alice.call(A, "grantAwards", {"awardIds": [21, 22]})
            alice.call(U, "SetActionCompleted", dict(WHERE, actionCode="3"))
            pid = alice.pid
            server.assert_clean()
        self.assertEqual(self.read("awards.json"), {str(pid): [21, 22]})
        self.assertEqual(self.read("uplay.json"), {str(pid): ["3"]})

        with Harness(data_dir=self.data_dir) as server:
            alice, bob = server.player("alice"), server.player("bob")
            self.assertEqual(alice.pid, pid)
            self.assertEqual(award_ids(alice.call(A, "getGrantedAwards", {
                "pid": pid, "resultRange": Awards.RANGE})), [21, 22])
            self.assertEqual(award_ids(bob.call(A, "getGrantedAwards", {
                "pid": bob.pid, "resultRange": Awards.RANGE})), [])
            self.assertEqual(completed(alice.call(U, "GetActions", EVERYTHING)["actions"]),
                             {"1": False, "2": False, "3": True, "4": False})
            server.assert_clean()

    def test_an_unreadable_file_is_an_error_but_awards_still_work(self):
        with open(os.path.join(self.data_dir, "awards.json"), "w", encoding="utf-8") as f:
            f.write("{not json")
        with Harness(data_dir=self.data_dir) as server:
            self.assertTrue(server.capture.has("starting with no granted awards"))
            alice = server.player("alice")
            alice.call(A, "grantAwards", {"awardIds": [1]})
            self.assertEqual(award_ids(alice.call(A, "getGrantedAwards", {
                "pid": alice.pid, "resultRange": Awards.RANGE})), [1])

    def test_a_server_without_a_data_directory_still_answers(self):
        with Harness(data_dir="") as server:
            alice = server.player("alice")
            alice.call(A, "grantAwards", {"awardIds": [1]})
            alice.call(U, "SetActionCompleted", dict(WHERE, actionCode="1"))
            self.assertEqual(award_ids(alice.call(A, "getGrantedAwards", {
                "pid": alice.pid, "resultRange": Awards.RANGE})), [1])
            server.assert_clean()


class Telemetry(LiveTest):
    INFO = {"ipn": 7, "userID": "alice", "machineID": "m-1", "visitorID": "v-1",
            "utsVersion": "1.0"}

    def test_every_tracking_call_is_taken_on_the_telemetry_connection(self):
        alice = self.player("alice")
        alice.login_telemetry()
        tel = alice.telemetry

        reply = alice._call(tel, T, "SendUserInfo", {"userInfo": self.INFO, "deltaTime": 3})
        self.assertEqual(reply["userInfo"], self.INFO)
        self.assertNotEqual(reply["trackingID"], 0)
        again = alice._call(tel, T, "SendUserInfo", {"userInfo": self.INFO, "deltaTime": 4})
        self.assertNotEqual(again["trackingID"], reply["trackingID"])

        tid = reply["trackingID"]
        self.assertEqual(alice._call(tel, T, "SendTag", {
            "trackingID": tid, "tag": "MENU", "attributes": "a=1", "deltaTime": 5}), {})
        self.assertEqual(alice._call(tel, T, "SendTagAndUpdateUserInfo", {
            "trackingID": tid, "tag": "LOGIN", "attributes": "", "deltaTime": 6,
            "userID": "alice"}), {})
        self.assertEqual(alice._call(tel, T, "SendTags", {"tagData": [
            {"trackingID": tid, "tag": "A", "attributes": "", "deltaTime": 7, "newUserId": ""},
            {"trackingID": tid, "tag": "B", "attributes": "x", "deltaTime": 8,
             "newUserId": ""}]}), {})
        self.assertEqual(alice._call(tel, T, "GetConfiguration"), {"tags": []})
        self.assertFalse(self.logged("not implemented"))

    def test_tracking_is_taken_on_the_players_own_connection_too(self):
        alice = self.player("alice")
        self.assertEqual(alice.call(T, "SendTag", {"trackingID": 1, "tag": "X"}), {})
        self.assertEqual(alice.call(T, "GetConfiguration"), {"tags": []})

    def test_nobody_is_a_vip(self):
        alice, bob = self.player("alice"), self.player("bob")
        self.assertEqual(alice.call("TrackingExtensionProtocol", "UserIsVIP",
                                    {"userId": str(alice.pid)}), {"isVIP": False})
        self.assertEqual(bob.call("TrackingExtensionProtocol", "UserIsVIP",
                                  {"userId": str(alice.pid)}), {"isVIP": False})

    def test_events_get_a_session_and_are_taken(self):
        alice, bob = self.player("alice"), self.player("bob")
        start = {"value%d" % n: n for n in range(1, 6)}
        first = alice.call("EventsTrackingProtocol", "StartSession", start)["sessionId"]
        second = bob.call("EventsTrackingProtocol", "StartSession", start)["sessionId"]
        self.assertNotEqual(first, 0)
        self.assertNotEqual(first, second)
        event = {"eventId": 12, "sessionId": first, "flag1": True, "text1": "a", "text2": "b"}
        event.update({"value%d" % n: n for n in range(3, 21)})
        self.assertEqual(alice.call("EventsTrackingProtocol", "AddEvent", event), {})
        self.assertTrue(self.wait_logged("event 12 in session %d" % first))


class Locale(LiveTest):
    def test_the_locale_is_remembered_per_player(self):
        alice, bob = self.player("alice"), self.player("bob")
        self.assertEqual(alice.call("LocalizationProtocol", "GetLocaleCode"),
                         {"localCode": "en-US"})
        self.assertEqual(alice.call("LocalizationProtocol", "SetLocaleCode",
                                    {"localCode": "fr-FR"}), {})
        self.assertEqual(alice.call("LocalizationProtocol", "GetLocaleCode"),
                         {"localCode": "fr-FR"})
        self.assertEqual(bob.call("LocalizationProtocol", "GetLocaleCode"),
                         {"localCode": "en-US"})

    def test_an_empty_locale_is_accepted_and_changes_nothing(self):
        alice = self.player("alice")
        alice.call("LocalizationProtocol", "SetLocaleCode", {"localCode": "de-DE"})
        self.assertEqual(alice.call("LocalizationProtocol", "SetLocaleCode",
                                    {"localCode": ""}), {})
        self.assertEqual(alice.call("LocalizationProtocol", "GetLocaleCode"),
                         {"localCode": "de-DE"})


class TitleStorage(LiveTest):
    S = "TitleStorageProtocol"

    def test_there_is_no_remote_game_config(self):
        # The exact call the game makes; with nothing found it keeps DLC\dlc\dlc.big.
        alice = self.player("alice")
        self.assertEqual(alice.call(self.S, "BrowseContentsByType", {
            "contentType": "GameConfigContent", "flags": 0xFFFFFFFF, "flagsMaskType": 1,
            "extendedFlags": 0xFFFFFFFF, "extendedFlagsMaskType": 1}), {"contents": []})
        self.assertEqual(alice.call(self.S, "BrowseContents", {
            "flags": 0xFFFFFFFF, "flagsMaskType": 1, "extendedFlags": 0xFFFFFFFF,
            "extendedFlagsMaskType": 1}), {"contents": []})

    def test_no_content_id_names_anything(self):
        alice = self.player("alice")
        for method in ("GetContentDetails", "DownloadContent"):
            with self.assertRaises(RmcFailure) as caught:
                alice.call(self.S, method, {"contentID": 1})
            self.assertEqual(caught.exception.code, 0x80150004, method)


class KeyActivation(LiveTest):
    KEY = "QQZX-7731-KEYW-0RDS"

    def test_a_key_is_refused_as_invalid(self):
        alice = self.player("alice")
        with self.assertRaises(RmcFailure) as caught:
            alice.call(P, "ActivateKey", {"uniqueKey": self.KEY, "languageCode": "en"})
        self.assertEqual(caught.exception.code, 0x800B0004)
        with self.assertRaises(RmcFailure) as caught:
            alice.call(P, "ActivateKeyWithExpectedPrivileges", {
                "uniqueKey": self.KEY, "languageCode": "en",
                "expectedPrivilegeIDs": [0x1432, 7]})
        self.assertEqual(caught.exception.code, 0x800B0004)
        self.assertTrue(self.wait_logged("privilege(s) 0x1432, 0x7"))
        # The connection is no worse for it.
        self.assertTrue(alice.call(P, "GetPrivilegesEx", {"localeCode": "en"})["privilegesEx"])

    def test_the_key_is_neither_logged_nor_stored(self):
        alice = self.player("alice")
        for method in ("ActivateKey", "ActivateKeyWithExpectedPrivileges"):
            with self.assertRaises(RmcFailure):
                alice.call(P, method, {"uniqueKey": self.KEY, "languageCode": "en"})
        self.assertTrue(self.wait_logged("PrivilegesProtocol.ActivateKeyWithExpectedPrivileges"))
        self.on_server(self.server.backend.save)
        hexed = self.KEY.encode().hex()
        spaced = self.KEY.encode().hex(" ")
        for line in self.server.capture.lines():
            self.assertNotIn(self.KEY, line)
            self.assertNotIn(hexed, line.replace(" ", ""))
            self.assertNotIn(spaced, line)
        for name in os.listdir(self.server.data_dir):
            with open(os.path.join(self.server.data_dir, name), encoding="utf-8") as f:
                self.assertNotIn(self.KEY, f.read())


class WireLayouts(LiveTest):
    """The Hermes layouts byte by byte, written the way the client's stubs write them and read
    the way its extractors read them. The rest of this module goes through the same declarations
    the server uses, so only these would catch a field out of place - which the game does not
    survive."""

    def raw(self, player, protocol, method_id, request: Writer) -> Reader:
        reply = player.call_raw(protocol, method_id, request.bytes())
        self.assertTrue(reply.success, "%s %d -> 0x%08X" % (protocol, method_id, reply.error))
        self.assertEqual(reply.method, method_id)
        return Reader(reply.body)

    def done(self, r: Reader):
        self.assertEqual(r.remaining(), 0, "bytes left after the client's extractor")

    @staticmethod
    def uplay_query() -> Writer:
        # C94F30 / C950F0: u32, u32, then three strings.
        return Writer().u32(0).u32(100).string("GRFS").string("en-US").string("PC")

    def read_action(self, r: Reader) -> dict:
        # 483580 / 483BE0: String x3, u32, String, then 4854B0: list of {String, bool, String}.
        a = {"code": r.string(), "name": r.string(), "description": r.string(),
             "value": r.u32(), "game": r.string()}
        a["platforms"] = [(r.string(), r.bool(), r.string()) for _ in range(r.u32())]
        return a

    def read_actions(self, r: Reader) -> list:
        return [self.read_action(r) for _ in range(r.u32())]

    def test_uplay_rewards_as_483CF0_reads_them(self):
        r = self.raw(self.player("alice"), U, 5, self.uplay_query())
        rewards = []
        for _ in range(r.u32()):
            # String x3, u32, String x2, then 4855C0: list of {String, bool}.
            code, _name, _text, value, _kind, _game = (r.string(), r.string(), r.string(),
                                                       r.u32(), r.string(), r.string())
            platforms = [(r.string(), r.bool()) for _ in range(r.u32())]
            rewards.append((code, value, platforms))
        self.done(r)
        self.assertEqual([c for c, _, _ in rewards], REWARD_CODES)
        for _, _, platforms in rewards:
            self.assertIn(("PC", True), platforms)

    def test_uplay_action_lists_as_4834D0_reads_them(self):
        alice = self.player("alice")
        r = self.raw(alice, U, 7, Writer().string("en-US").string("PC"))
        self.assertEqual(self.read_actions(r), [])
        self.done(r)
        # 483E20: a u32 count, then each code as a String.
        r = self.raw(alice, U, 9, Writer().u32(2).string("3").string("nope")
                     .string("en-US").string("PC"))
        self.assertEqual([a["code"] for a in self.read_actions(r)], ["3"])
        self.done(r)
        r = self.raw(alice, U, 1, self.uplay_query())
        actions = self.read_actions(r)
        self.done(r)
        self.assertEqual([a["code"] for a in actions], ["1", "2", "3", "4"])
        self.assertEqual([a["value"] for a in actions], [10, 20, 30, 40])
        self.assertIn(("PC", True, ""), actions[2]["platforms"])

    def test_uplay_unlock_as_483580_reads_it(self):
        alice = self.player("alice")
        r = self.raw(alice, U, 8, Writer().string("4").string("en-US").string("PC"))
        action = self.read_action(r)
        self.done(r)
        self.assertEqual((action["code"], action["value"]), ("4", 40))
        r = self.raw(alice, U, 8, Writer().string("99").string("en-US").string("PC"))
        self.assertEqual(self.read_action(r)["value"], 0xFFFFFFFF)  # A44100: not announced
        self.done(r)

    def test_awards_as_8E34B0_reads_them(self):
        alice = self.player("alice")
        # 8E23E0: 55DDE0, a u32 count then each u32; nothing comes back.
        r = self.raw(alice, A, 3, Writer().u32(2).u32(31).u32(32))
        self.done(r)
        # 8E22C0: u32 pid, then ResultRange {u32 offset, u32 size}.
        r = self.raw(alice, A, 2, Writer().u32(alice.pid).u32(0).u32(150))
        self.assertEqual([(r.u32(), r.u32()) for _ in range(r.u32())], [(31, 0), (32, 0)])
        self.done(r)

    def test_telemetry_extras_as_their_dispatchers_read_them(self):
        alice = self.player("alice")
        # 8E1970 sends the pid as decimal text; 8E1C30 reads one byte.
        r = self.raw(alice, "TrackingExtensionProtocol", 1, Writer().string(str(alice.pid)))
        self.assertEqual(r.u8(), 0)
        self.done(r)
        # C8A640: five u32; C8A1F0 reads one u32.
        start = Writer()
        for n in range(5):
            start.u32(n)
        r = self.raw(alice, "EventsTrackingProtocol", 1, start)
        self.assertNotEqual(r.u32(), 0)
        self.done(r)
        # C8A330: twenty u32, six bools, two strings; nothing comes back.
        event = Writer()
        for n in range(20):
            event.u32(n)
        for n in range(6):
            event.bool(n % 2)
        event.string("a").string("b")
        r = self.raw(alice, "EventsTrackingProtocol", 2, event)
        self.done(r)
        self.assertFalse(self.logged("bytes past the end"))


if __name__ == "__main__":
    unittest.main()
