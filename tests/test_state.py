"""The server's state, one piece at a time: settings, the register, friendships, connections."""

import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from grfs import friends as model
from grfs.accounts import FIRST_PID, Accounts
from grfs.backend import FORGET_AFTER, PRESENCE_WINDOW, Backend
from grfs.config import Config
from grfs.kerberos import SERVER_PID, TRACKING_NAME, TRACKING_PID
from grfs.prudp.connection import ANSWERS_KEPT, Client, ClientTable, Link


class TempDir(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="grfs-test-")
        self.addCleanup(shutil.rmtree, self.dir, True)

    def path(self, name):
        return os.path.join(self.dir, name)

    def write(self, name, text):
        with open(self.path(name), "w", encoding="utf-8") as f:
            f.write(text)
        return self.path(name)


class Settings(TempDir):
    def test_defaults(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            c = Config(path=self.path("missing.ini"))
        self.assertIsNone(c.path)
        self.assertEqual(c.int("auth_port"), 21030)
        self.assertEqual(c.int("secure_port"), 21031)
        self.assertEqual(c.int("onlineconfig_port"), 80)
        self.assertEqual(c.get("bind"), "0.0.0.0")
        self.assertEqual(c.get("advertise"), "")

    def test_the_ini_then_the_environment_then_the_command_line(self):
        ini = self.write("grfs.ini", "[server]\nauth_port = 1000\nsecure_port = 1001\n"
                                     "advertise = 10.0.0.1\n")
        with mock.patch.dict(os.environ, {"GRFS_SECURE_PORT": "2001", "GRFS_ADVERTISE": "x"},
                             clear=True):
            c = Config(path=ini, overrides={"advertise": "10.9.9.9"})
        self.assertEqual(c.path, ini)
        self.assertEqual(c.int("auth_port"), 1000)
        self.assertEqual(c.int("secure_port"), 2001)
        self.assertEqual(c.get("advertise"), "10.9.9.9")

    def test_an_ini_without_the_section_changes_nothing(self):
        ini = self.write("grfs.ini", "[other]\nauth_port = 5\n")
        self.assertEqual(Config(path=ini).int("auth_port"), 21030)

    def test_numbers_and_switches(self):
        c = Config(path=os.devnull, overrides={"a": "0x10", "b": "junk", "c": " 7 ",
                                               "on": "Yes", "off": "0"})
        self.assertEqual(c.int("a"), 16)
        self.assertEqual(c.int("b", 3), 3)
        self.assertEqual(c.int("c"), 7)
        self.assertEqual(c.int("missing", 9), 9)
        self.assertTrue(c.bool("on"))
        self.assertFalse(c.bool("off"))
        self.assertFalse(c.bool("missing"))
        self.assertTrue(c.bool("missing", True))
        for word in ("1", "true", "ON"):
            self.assertTrue(Config(path=os.devnull, overrides={"x": word}).bool("x"))

    def test_describe_names_the_ports(self):
        text = Config(path=os.devnull, overrides={"advertise": ""}).describe()
        self.assertIn("auth_port=21030", text)
        self.assertIn("advertise=-", text)


class Register(TempDir):
    def test_pids_start_at_the_first_and_count_up(self):
        a = Accounts()
        self.assertEqual(a.pid_for("alice"), FIRST_PID)
        self.assertEqual(a.pid_for("bob"), FIRST_PID + 1)
        self.assertEqual(a.pid_for("ALICE"), FIRST_PID)
        self.assertEqual(len(a), 2)
        self.assertEqual(a.name_for(FIRST_PID + 1), "bob")
        self.assertEqual(a.name_for(0x9999), "")

    def test_the_telemetry_user_is_never_registered(self):
        a = Accounts()
        self.assertEqual(a.pid_for(TRACKING_NAME), TRACKING_PID)
        self.assertEqual(a.name_for(TRACKING_PID), TRACKING_NAME)
        self.assertEqual(len(a), 0)

    def test_reserved_pids_are_skipped(self):
        a = Accounts()
        a._next_pid = SERVER_PID
        self.assertEqual(a.pid_for("x"), SERVER_PID + 1)
        a._next_pid = TRACKING_PID
        self.assertEqual(a.pid_for("y"), TRACKING_PID + 1)

    def test_it_survives_a_restart_and_never_reuses_a_pid(self):
        path = self.path("accounts.json")
        a = Accounts(path)
        a.pid_for("alice")
        a.pid_for("bob")
        b = Accounts(path)
        self.assertEqual(b.get("Bob").pid, FIRST_PID + 1)
        self.assertEqual(b.pid_for("carol"), FIRST_PID + 2)
        with open(path, encoding="utf-8") as f:
            rows = json.load(f)
        self.assertEqual([r["name"] for r in rows], ["alice", "bob", "carol"])

    def test_a_damaged_file_starts_an_empty_register(self):
        path = self.write("accounts.json", "{not json")
        with self.assertLogs("grfs.accounts", "ERROR"):
            a = Accounts(path)
        self.assertEqual(len(a), 0)
        self.assertEqual(a.pid_for("alice"), FIRST_PID)

    def test_with_no_file_nothing_is_written(self):
        a = Accounts(None)
        a.pid_for("alice")
        a.save()
        self.assertEqual(os.listdir(self.dir), [])


class Friendships(TempDir):
    def setUp(self):
        super().setUp()
        self.accounts = Accounts()
        for name in ("alice", "bob", "carol"):
            self.accounts.pid_for(name)
        self.f = model.Friends(self.accounts, self.path("friends.json"))
        self.told = []
        self.f.on_change = lambda to, by, kind: self.told.append((self.name_of(to),
                                                                   self.name_of(by), kind))

    def name_of(self, pid):
        return self.accounts.name_for(pid)

    def rel(self, a, b):
        return self.f.relationship(a, b)

    def names(self, contacts):
        return [(c.name, c.relationship) for c in contacts]

    def test_a_request_has_two_sides_and_is_idempotent(self):
        self.assertTrue(self.f.request("alice", "bob", "hi", 5))
        self.assertTrue(self.f.request("alice", "bob", "hi again"))
        self.assertEqual((self.rel("alice", "bob"), self.rel("bob", "alice")),
                         (model.REQUEST_SENT, model.REQUEST_RECEIVED))
        self.assertEqual(self.f.messages("bob"), [(self.accounts.get("alice").pid, "hi again")])
        self.assertEqual(self.f.edge("alice", "bob").details, 1)
        # Asking again without text keeps the text.
        self.assertTrue(self.f.request("alice", "bob", ""))
        self.assertEqual(self.f.messages("bob"), [(self.accounts.get("alice").pid, "hi again")])
        self.assertEqual(self.told, [("bob", "alice", model.NEW_REQUEST),
                                     ("bob", "alice", model.SOMETHING_CHANGED)])

    def test_names_are_matched_whatever_the_case(self):
        self.f.request("Alice", "BOB")
        self.assertEqual(self.rel("alice", "bob"), model.REQUEST_SENT)
        self.assertEqual([c.name for c in self.f.contacts("ALICE")], ["bob"])

    def test_nobody_befriends_themselves_or_a_stranger(self):
        self.assertFalse(self.f.request("alice", "alice"))
        self.assertFalse(self.f.request("alice", "ghost"))
        self.assertFalse(self.f.request("", "bob"))
        self.assertFalse(self.f.block("alice", "ghost"))
        self.assertFalse(self.f.unblock("alice", "ghost"))
        self.assertEqual(self.f.edges, {})
        self.assertEqual(self.told, [])

    def test_accepting_needs_a_request(self):
        self.assertFalse(self.f.accept("bob", "alice"))
        self.f.request("alice", "bob")
        self.assertFalse(self.f.accept("alice", "bob"))      # the asker cannot accept
        self.assertTrue(self.f.accept("bob", "alice"))
        self.assertEqual(self.f.messages("bob"), [])
        self.assertTrue(self.f.request("alice", "bob"))      # already friends: still yes
        self.assertEqual(self.rel("bob", "alice"), model.FRIEND)
        self.assertEqual(self.names(self.f.friends_of("alice")), [("bob", model.FRIEND)])
        # Told: bob of the request, alice of the answer. Nothing for the repeat.
        self.assertEqual(self.told, [("bob", "alice", model.NEW_REQUEST),
                                     ("alice", "bob", model.SOMETHING_CHANGED)])

    def test_ending_it(self):
        self.assertFalse(self.f.clear("alice", "bob"))
        self.f.request("alice", "bob")
        self.assertFalse(self.f.decline("alice", "bob"))     # only the one asked declines
        self.assertTrue(self.f.decline("bob", "alice"))
        self.assertEqual(self.f.contacts("alice"), [])
        self.assertEqual(len(self.f.edges), 2)                # kept, as "none"
        self.assertEqual(self.told[-1], ("alice", "bob", model.SOMETHING_CHANGED))
        self.f.request("alice", "bob")
        self.f.accept("bob", "alice")
        self.assertFalse(self.f.decline("bob", "alice"))     # a friend is removed, not declined
        self.assertTrue(self.f.clear("bob", "alice"))
        self.assertEqual(self.told[-1], ("alice", "bob", model.SOMETHING_CHANGED))

    def test_blocking_and_unblocking(self):
        self.f.request("alice", "bob")
        self.f.accept("bob", "alice")
        self.told.clear()
        self.assertTrue(self.f.block("alice", "bob"))
        self.assertTrue(self.f.blocked("alice", "bob"))
        self.assertFalse(self.f.blocked("bob", "alice"))
        # The blocked player is on the blacklist and nowhere else, and the blocker is on none of
        # theirs.
        self.assertEqual(self.f.contacts("alice"), [])
        self.assertEqual([(c.name, c.blocked) for c in self.f.blacklist("alice")],
                         [("bob", True)])
        self.assertEqual(self.f.contacts("bob"), [])
        self.assertEqual(self.f.blacklist("bob"), [])
        self.assertEqual(self.told, [("bob", "alice", model.SOMETHING_CHANGED)])
        self.assertFalse(self.f.request("bob", "alice"))
        self.assertTrue(self.f.clear("alice", "bob"))         # BLOCKLIST "Remove"
        self.assertEqual(self.f.blacklist("alice"), [])
        self.assertFalse(self.f.unblock("alice", "bob"))
        self.f.block("alice", "bob")
        self.assertTrue(self.f.unblock("alice", "bob"))
        self.assertFalse(self.f.unblock("alice", "bob"))
        self.assertFalse(self.f.unblock("alice", "carol"))
        self.assertEqual(len(self.told), 1, "nothing on bob's lists changed after the block")
        self.assertTrue(self.f.request("bob", "alice"))

    def test_asking_somebody_you_blocked_unblocks_them(self):
        self.f.block("alice", "bob")
        self.assertTrue(self.f.request("alice", "bob", ""))
        self.assertFalse(self.f.blocked("alice", "bob"))
        self.assertEqual(self.names(self.f.contacts("bob")), [("alice", model.REQUEST_RECEIVED)])
        self.assertEqual(self.told, [("bob", "alice", model.NEW_REQUEST)])

    def test_somebody_who_blocked_you_is_on_none_of_your_lists(self):
        # Even with an edge left over from before blocking was kept apart in this file.
        self.f.request("alice", "bob")
        self.f.edge("bob", "alice").blocked = True
        self.assertEqual(self.f.contacts("alice"), [])
        self.assertEqual(self.f.blacklist("alice"), [])
        self.assertEqual(self.f.messages("bob"), [])
        self.assertIsNone(self.f.view("alice", "bob"))
        self.assertEqual(self.f.view("bob", "alice"), model.BLOCKED)

    def test_a_notification_that_fails_does_not_undo_the_change(self):
        def broken(*args):
            raise RuntimeError("no network")
        self.f.on_change = broken
        with self.assertLogs("grfs.friends", "ERROR"):
            self.assertTrue(self.f.request("alice", "bob"))
        self.assertEqual(self.rel("bob", "alice"), model.REQUEST_RECEIVED)

    def test_details_and_messages(self):
        self.assertFalse(self.f.set_details("alice", "bob", 3))
        self.f.request("alice", "bob", "text")
        self.assertTrue(self.f.set_details("alice", "bob", 3))
        self.assertEqual(self.f.edge("alice", "bob").details, 3)
        alice = self.accounts.get("alice").pid
        self.assertTrue(self.f.drop_message("bob", alice))
        self.assertFalse(self.f.drop_message("bob", alice))
        self.assertFalse(self.f.drop_message("bob", 0x9999))
        self.assertEqual(self.rel("bob", "alice"), model.REQUEST_RECEIVED)
        # Neither is anything the other player sees.
        self.assertEqual(self.told, [("bob", "alice", model.NEW_REQUEST)])

    def test_search(self):
        self.assertEqual([a.name for a in self.f.search("O")], ["bob", "carol"])
        self.assertEqual([a.name for a in self.f.search("", excluding="Bob")], ["alice", "carol"])
        self.assertEqual(self.f.search("zzz"), [])
        self.assertEqual(len(self.f.search(None)), 3)

    def test_it_survives_a_restart(self):
        self.f.request("alice", "bob", "hi", 4)
        self.f.block("carol", "alice")
        again = model.Friends(self.accounts, self.path("friends.json"))
        self.assertEqual(again.relationship("bob", "alice"), model.REQUEST_RECEIVED)
        self.assertEqual(again.edge("bob", "alice").message, "hi")
        self.assertEqual(again.edge("alice", "bob").details, 4)
        self.assertTrue(again.blocked("carol", "alice"))
        self.assertEqual([c.name for c in again.blacklist("carol")], ["alice"])

    def test_a_damaged_file_starts_with_nobody_knowing_anybody(self):
        path = self.write("broken.json", "[{")
        with self.assertLogs("grfs.friends", "ERROR"):
            f = model.Friends(self.accounts, path)
        self.assertEqual(f.edges, {})

    def test_an_edge_to_an_account_that_is_gone_is_not_shown(self):
        self.f.request("alice", "bob")
        self.f.block("alice", "carol")
        del self.accounts.by_name["bob"]
        del self.accounts.by_name["carol"]
        self.assertEqual(self.f.contacts("alice"), [])
        self.assertEqual(self.f.blacklist("alice"), [])
        self.assertEqual(self.f.contacts(""), [])
        self.assertEqual(self.f.blacklist(""), [])

    def test_everyone_mode(self):
        f = model.Friends(self.accounts, None, everyone=True)
        told = []
        f.on_change = lambda to, by, kind: told.append((self.name_of(to), self.name_of(by)))
        self.assertEqual(self.names(f.contacts("alice")),
                         [("bob", model.FRIEND), ("carol", model.FRIEND)])
        # Asking, answering and removing change nothing here.
        self.assertTrue(f.request("alice", "bob"))
        self.assertTrue(f.accept("bob", "alice"))
        self.assertFalse(f.decline("bob", "alice"))
        self.assertFalse(f.clear("alice", "bob"))
        self.assertEqual(told, [])
        # Blocking does, both ways, and the game's Unblock undoes it.
        f.block("alice", "carol")
        self.assertEqual([c.name for c in f.contacts("alice")], ["bob"])
        self.assertEqual([c.name for c in f.contacts("carol")], ["bob"])
        self.assertEqual([c.name for c in f.blacklist("alice")], ["carol"])
        self.assertFalse(f.accept("carol", "alice"))
        self.assertTrue(f.request("alice", "carol"))
        self.assertEqual([c.name for c in f.contacts("carol")], ["alice", "bob"])
        self.assertEqual(told, [("carol", "alice"), ("carol", "alice")])
        f.block("alice", "carol")
        self.assertTrue(f.clear("alice", "carol"))
        self.assertEqual(f.blacklist("alice"), [])


class Links(unittest.TestCase):
    def test_numbering(self):
        link = Link(21031, 0x3F)
        self.assertEqual([link.next_reliable() for _ in range(3)], [1, 2, 3])
        self.assertEqual(link.next_unreliable(), 1)
        self.assertEqual(link.next_unreliable(after=10), 11)
        self.assertEqual(link.next_unreliable(after=5), 12)
        link.saw(40)
        self.assertEqual(link.next_unreliable(), 41)
        link.saw(3)
        self.assertEqual(link.next_unreliable(), 42)

    def test_answers_are_recorded_while_answering_only(self):
        link = Link(21031, 0x3F)
        link.record(b"before")
        self.assertIsNone(link.answer_to(2))
        link.begin_answer(2)
        link.record(b"a")
        link.record(b"b")
        link.end_answer()
        link.record(b"after")
        self.assertEqual(link.answer_to(2), [b"a", b"b"])

    def test_only_so_many_answers_are_kept(self):
        link = Link(21031, 0x3F)
        for seq in range(ANSWERS_KEPT + 10):
            link.begin_answer(seq)
            link.end_answer()
        self.assertIsNone(link.answer_to(9))
        self.assertEqual(link.answer_to(10), [])
        self.assertEqual(link.answer_to(ANSWERS_KEPT + 9), [])

    def test_a_restart_forgets_everything_but_who_it_is(self):
        link = Link(21031, 0x3F)
        link.signature = 0xAA
        link.next_reliable()
        link.next_unreliable()
        link.data_seen = True
        link.fragments[3] = (1, b"x")
        link.begin_answer(2)
        link.restart()
        self.assertEqual((link.reliable_seq, link.out_seq, link.data_seen, link.fragments),
                         (0, 0, False, {}))
        self.assertIsNone(link.answer_to(2))
        self.assertEqual(link.signature, 0xAA)
        self.assertIn("3F", repr(link))


class Clients(unittest.TestCase):
    def test_signature_falls_back_to_another_connection_to_the_same_socket(self):
        c = Client(1, ("1.2.3.4", 5))
        self.assertEqual(c.signature_for(21031, 0x3F), 0)
        c.link(21031, 0x3E).signature = 0x1111
        c.link(21030, 0x3F).signature = 0x2222
        self.assertEqual(c.signature_for(21031, 0x3F), 0x1111)
        c.link(21031, 0x3F).signature = 0x3333
        self.assertEqual(c.signature_for(21031, 0x3F), 0x3333)
        self.assertIs(c.find_link(21031, 0x3F), c.link(21031, 0x3F))
        self.assertIsNone(c.find_link(1, 1))
        self.assertEqual([c.next_call_id(), c.next_call_id()], [1, 2])
        self.assertEqual(c.address, "1.2.3.4")

    def test_the_table(self):
        t = ClientTable()
        a = t.for_endpoint(("1.1.1.1", 1))
        self.assertIs(t.for_endpoint(("1.1.1.1", 1)), a)
        self.assertEqual(a.id, ClientTable.FIRST_ID)
        b = t.for_endpoint(("1.1.1.1", 2))
        self.assertEqual(b.id, ClientTable.FIRST_ID + 1)
        self.assertIs(t.by_signature(b.id), b)
        a.pid = b.pid = 0x1234
        a.name = "Alice"
        a.last_seen, b.last_seen = 100, 200
        self.assertIs(t.by_pid(0x1234), b)
        self.assertIsNone(t.by_pid(0x9999))
        self.assertIs(t.by_name("alice"), a)
        self.assertIsNone(t.by_name(""))
        self.assertEqual(t.drop_stale_with_pid(b), [a])
        self.assertEqual(list(t), [b])
        self.assertIsNone(t.by_signature(a.id))
        self.assertEqual(t.reap(50, 300), [b])
        self.assertEqual(len(t), 0)


class BackendOddsAndEnds(unittest.TestCase):
    def setUp(self):
        self.backend = Backend(config=Config(path=os.devnull, overrides={"advertise": ""}),
                               data_dir="")

    def test_a_local_game_is_told_127_0_0_1(self):
        self.assertEqual(self.backend.address_for("127.0.0.1"), "127.0.0.1")
        self.assertEqual(self.backend.address_for("127.5.5.5"), "127.0.0.1")
        self.assertEqual(self.backend.address_for("::1"), "127.0.0.1")

    def test_a_game_on_the_network_is_told_the_address_that_routes_to_it(self):
        with self.assertLogs("grfs.backend", "INFO"):
            told = self.backend.address_for("192.0.2.10")
        self.assertNotEqual(told, "0.0.0.0")
        self.assertEqual(self.backend._advertised["192.0.2.10"], told)
        self.assertEqual(self.backend.address_for("192.0.2.10"), told)       # cached

    def test_the_advertised_address_wins_for_the_network(self):
        b = Backend(config=Config(path=os.devnull, overrides={"advertise": "198.51.100.1"}),
                    data_dir="")
        self.assertEqual(b.address_for("192.0.2.10"), "198.51.100.1")
        self.assertEqual(b.address_for("127.0.0.1"), "127.0.0.1")

    def test_urls(self):
        client = self.backend.clients.for_endpoint(("127.0.0.1", 3000))
        client.pid, client.rvcid = 0x1234, 80
        self.assertEqual(self.backend.secure_url(client),
                         "prudps:/address=127.0.0.1;port=21031;CID=1;PID=4096;sid=1;stream=3;type=2")
        self.assertEqual(self.backend.station_url(client),
                         "prudps:/address=127.0.0.1;port=2347;CID=1;PID=4660;sid=1;RVCID=80;"
                         "stream=3;type=2")
        self.assertIn("RVCID=81", self.backend.station_url(client, 81))
        self.assertEqual(self.backend.server_vport(0x3F), 0x31)
        self.assertEqual(self.backend.server_vport(0x5E), 0x51)

    def test_presence_and_forgetting(self):
        client = self.backend.clients.for_endpoint(("127.0.0.1", 3000))
        client.pid = 0x1234
        now = client.last_seen
        self.assertFalse(self.backend.is_online(0))
        with mock.patch.object(self.backend, "now", return_value=now + PRESENCE_WINDOW - 1):
            self.assertTrue(self.backend.is_online(0x1234))
        with mock.patch.object(self.backend, "now", return_value=now + PRESENCE_WINDOW + 1):
            self.assertFalse(self.backend.is_online(0x1234))
            self.assertEqual(self.backend.forget_quiet_clients(), [])
        with mock.patch.object(self.backend, "now", return_value=now + FORGET_AFTER + 1):
            with self.assertLogs("grfs.backend", "INFO"):
                self.assertEqual(self.backend.forget_quiet_clients(), [client])
        self.assertFalse(self.backend.is_online(0x1234))

    def test_status(self):
        self.backend.accounts.pid_for("alice")
        client = self.backend.clients.for_endpoint(("127.0.0.1", 3000))
        client.name, client.pid, client.urls = "alice", 0x1234, ["prudp:/x"]
        unnamed = self.backend.clients.for_endpoint(("127.0.0.1", 3001))
        status = self.backend.status()
        self.assertEqual(status["accounts"], 1)
        rows = {c["address"]: c for c in status["clients"]}
        self.assertEqual(rows["127.0.0.1:3000"]["urls"], ["prudp:/x"])
        self.assertEqual(rows["127.0.0.1:3001"]["friends"], [])
        self.assertEqual(rows["127.0.0.1:3001"]["name"], unnamed.name)
        self.assertEqual(status["packets"], {})
        json_ready = json.dumps(status)
        self.assertTrue(json_ready)

    def test_account_ids_stand_in_for_names(self):
        self.backend.accounts.pid_for("Alice")
        self.assertEqual(self.backend.ubi_account_id("alice"), "Alice")
        self.assertEqual(self.backend.ubi_account_id("ghost"), "ghost")
        self.assertEqual(self.backend.ubi_account_id(None), "")
        self.assertEqual(self.backend.name_for_ubi_account_id("ALICE"), "Alice")
        self.assertEqual(self.backend.name_for_ubi_account_id("ghost"), "")

    def test_every_service_is_installed_and_in_the_schema(self):
        for service in self.backend.router.services.values():
            self.assertIn(service.protocol_name, self.backend.schema.protocols,
                          service.protocol_name)
            self.assertTrue(service.handled(), service.protocol_name)
            # Every method a service defines in the protocol's style is one the schema has.
            names = {m.name for m in self.backend.schema.protocols[service.protocol_name]
                     .methods.values()}
            mine = {n for n in dir(service) if n[:1].isupper() and callable(getattr(service, n))}
            self.assertEqual(mine - names, set(), service.protocol_name)
