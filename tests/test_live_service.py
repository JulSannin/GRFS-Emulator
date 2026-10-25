"""The rest of a running server: its two HTTP services, what survives a restart, and starting up."""

import asyncio
import contextlib
import http.client
import io
import json
import logging
import os
import random
import shutil
import socket
import tempfile
import unittest

from grfs import friends as model
from grfs import log as logs
from grfs import punch
from grfs.__main__ import main, start
from grfs.config import Config
from grfs.harness import Harness
from grfs.onlineconfig import CONFIG_ID, PATH

from tests.live import LiveTest


def http_get(port, path, method="GET"):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.request(method, path, headers={"Host": "onlineconfigservice.ubi.com"})
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


class ConfigurationService(LiveTest):
    def port(self):
        return self.server.ports["onlineconfig"]

    def test_the_answer_looks_like_the_live_one(self):
        status, headers, body = http_get(self.port(), PATH + "?onlineConfigID=%s&target=client"
                                         % CONFIG_ID)
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "application/json; charset=utf-8")
        self.assertEqual(int(headers["Content-Length"]), len(body))
        # Slashes escaped, as the live service sends them.
        self.assertIn(b"prudp:\\/address=127.0.0.1", body)
        self.assertNotIn(b"prudp:/", body)
        rows = json.loads(body)
        self.assertEqual(rows[0], {"Name": "SandboxUrl", "Values": [
            "prudp:/address=127.0.0.1;port=%d" % self.server.ports["auth"]]})
        # One value each, except the NAT type detector's two servers (see tests.test_live_nat).
        self.assertTrue(all(len(r["Values"]) == 1 for r in rows
                            if r["Name"] != "punch_DetectUrls"))
        punch = self.server.ports["punch"]
        self.assertEqual(rows[-1], {"Name": "punch_DetectUrls", "Values": [
            "127.0.0.1:%d" % punch, "127.0.0.1:%d" % (punch + 1)]})

    def test_the_online_pass_is_switched_off(self):
        # The client only looks for the key; any value switches the pass check off.
        self.assertIn("PassportKillSwitch", self.server.config_service())

    def test_an_unknown_id_is_answered_all_the_same(self):
        values = self.server.config_service(config_id="0" * 32)
        self.assertIn("SandboxUrl", values)
        self.assertTrue(self.logged("a configuration id we do not know"))

    def test_anything_else_is_not_found(self):
        self.assertEqual(http_get(self.port(), "/favicon.ico")[0], 404)
        self.assertEqual(http_get(self.port(), PATH, method="POST")[0], 404)

    def test_a_connection_that_says_nothing_is_closed_without_harm(self):
        with socket.create_connection(("127.0.0.1", self.port()), timeout=5):
            pass
        self.assertIn("SandboxUrl", self.server.config_service())


class PunchDetect(LiveTest):
    settings = {"punch_detect_urls": "udp:/address=127.0.0.1;port=3074"}

    def test_the_old_experiment_is_gone(self):
        """It sent one udp:/ value, which the client can never use - it wants two host:port
        values. The setting is ignored now; the detector's own address is what is offered."""
        self.assertEqual(self.server.config_service()["punch_DetectUrls"],
                         "127.0.0.1:%d" % self.server.ports["punch"])


class Advertised(LiveTest):
    settings = {"advertise": "203.0.113.7"}

    def test_a_local_game_is_still_told_127_0_0_1(self):
        alice = self.player("alice")
        self.assertIn("address=127.0.0.1", alice.secure_url)
        self.assertEqual(self.server.backend.address_for("10.0.0.9"), "203.0.113.7")


class StatusPage(LiveTest):
    def port(self):
        return self.server.ports["panel"]

    def test_the_page(self):
        status, headers, body = http_get(self.port(), "/")
        self.assertEqual(status, 200)
        self.assertIn(b"<title>GRFS Emulator</title>", body)
        self.assertEqual(http_get(self.port(), "/index.html")[0], 200)

    def test_the_status_shows_who_is_here(self):
        alice = self.player("alice")
        bob = self.player("bob")
        alice.call("FriendsProtocol", "AddFriendByName", {"strPlayerName": "bob"})
        status, _, body = http_get(self.port(), "/api/status")
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertEqual(data["accounts"], 2)
        clients = {c["name"]: c for c in data["clients"]}
        self.assertEqual(clients["alice"]["pid"], alice.pid)
        self.assertEqual(clients["alice"]["address"], "127.0.0.1:%d" % alice.wire.port)
        self.assertEqual(clients["alice"]["friends"], [
            {"name": "bob", "pid": bob.pid, "relationship": model.REQUEST_SENT, "online": True}])
        self.assertGreater(data["packets"]["secure"]["in"], 0)
        self.assertGreater(data["packets"]["auth"]["out"], 0)
        self.assertIn("log", data)
        self.assertGreaterEqual(data["uptime"], 0)
        self.assertEqual(data["sessions"], [])

    def test_the_status_shows_blocks_and_sessions(self):
        alice = self.player("alice")
        bob = self.player("bob")
        bob.call("FriendsProtocol", "BlackListByName", {"strPlayerName": "alice", "uiDetails": 0})
        alice.call("GameSessionProtocol", "CreateSession", {"gameSession": {
            "m_typeID": 1, "m_attributes": [{"m_ID": 3, "m_value": 4},
                                            {"m_ID": 4, "m_value": 0}]}})
        data = json.loads(http_get(self.port(), "/api/status")[2])
        clients = {c["name"]: c for c in data["clients"]}
        self.assertEqual((clients["bob"]["blocked"], clients["bob"]["friends"]), (["alice"], []))
        self.assertEqual(clients["alice"]["blocked"], [])
        self.assertFalse(clients["alice"]["away"])
        [session] = data["sessions"]
        self.assertEqual((session["host_name"], session["capacity"]), ("alice", [4, 0]))

    def test_anything_else_is_not_found(self):
        self.assertEqual(http_get(self.port(), "/secret")[0], 404)


class Restarting(unittest.TestCase):
    def setUp(self):
        self.data_dir = tempfile.mkdtemp(prefix="grfs-test-")
        self.addCleanup(shutil.rmtree, self.data_dir, True)

    def test_accounts_and_friends_survive_a_restart(self):
        with Harness(data_dir=self.data_dir) as server:
            alice = server.player("alice")
            bob = server.player("bob")
            alice.call("FriendsProtocol", "AddFriendByName", {"strPlayerName": "bob"})
            bob.call("FriendsProtocol", "AcceptFriendship", {"uiPlayer": alice.pid})
            pids = alice.pid, bob.pid
            server.assert_clean()
        self.assertTrue(os.path.exists(os.path.join(self.data_dir, "accounts.json")))
        self.assertTrue(os.path.exists(os.path.join(self.data_dir, "friends.json")))

        with Harness(data_dir=self.data_dir) as server:
            alice = server.player("alice")
            self.assertEqual(alice.pid, pids[0])
            # Bob has not come back yet: still a friend, just offline.
            self.assertEqual(alice.relationships(), {"bob": (model.FRIEND, False)})
            self.assertEqual(server.player("carol").pid, pids[1] + 1)
            server.assert_clean()

    def test_a_server_without_a_data_directory_remembers_nothing(self):
        with Harness(data_dir="") as server:
            server.player("alice")
            self.assertIsNone(server.backend.accounts.path)
            self.assertIsNone(server.backend.friends.path)
            server.assert_clean()


class StartingUp(unittest.TestCase):
    @staticmethod
    def free_ports(n, kind=socket.SOCK_DGRAM):
        socks = [socket.socket(socket.AF_INET, kind) for _ in range(n)]
        for s in socks:
            s.bind(("127.0.0.1", 0))
        ports = [s.getsockname()[1] for s in socks]
        for s in socks:
            s.close()
        return ports

    @staticmethod
    def free_pair():
        """Two consecutive free UDP ports, for the NAT type detector - from where the harness
        looks for them, for the same reason (grfs.punch.ANY_PORTS)."""
        for _ in range(punch.ANY_PORT_ATTEMPTS):
            port = random.randrange(*punch.ANY_PORTS)
            first = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            second = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                first.bind(("127.0.0.1", port))
                second.bind(("127.0.0.1", port + 1))
                return port
            except OSError:
                continue
            finally:
                first.close()
                second.close()
        raise OSError("no two free UDP ports in a row")

    def test_the_configured_ports_are_the_ones_used(self):
        auth, secure = self.free_ports(2)
        config_port, = self.free_ports(1, socket.SOCK_STREAM)
        punch = self.free_pair()
        config = Config(path=os.devnull, overrides={
            "bind": "127.0.0.1", "auth_port": str(auth), "secure_port": str(secure),
            "onlineconfig_port": str(config_port), "punch_port": str(punch),
            "panel_port": "0"})

        async def run():
            running = await start(config, data_dir="")
            try:
                return dict(running.ports), running.describe()
            finally:
                await running.stop()
        ports, text = asyncio.run(run())
        self.assertEqual(ports, {"onlineconfig": config_port, "auth": auth, "secure": secure,
                                 "punch": punch, "panel": 0})
        self.assertIn("auth=%d" % auth, text)
        self.assertIn("punch=%d" % punch, text)

    def test_the_detector_can_be_switched_off(self):
        config = Config(path=os.devnull, overrides={"bind": "127.0.0.1", "punch_port": "0"})

        async def run():
            running = await start(config, data_dir="", any_port=True)
            try:
                return dict(running.ports), "punch" in running.backend.servers
            finally:
                await running.stop()
        ports, listed = asyncio.run(run())
        self.assertEqual(ports["punch"], 0)
        self.assertFalse(listed)

    def test_a_punch_port_already_taken_stops_the_start(self):
        """Like any other port: the game reads the configuration once, so a detector that is
        not where the configuration says would only make every game wait for nothing."""
        punch = self.free_pair()
        taken = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        taken.bind(("127.0.0.1", punch + 1))
        self.addCleanup(taken.close)
        auth, secure = self.free_ports(2)
        config = Config(path=os.devnull, overrides={
            "bind": "127.0.0.1", "auth_port": str(auth), "secure_port": str(secure),
            "punch_port": str(punch), "panel_port": "0"})

        async def run():
            running = await start(config, data_dir="", any_port=False)
            await running.stop()
        with self.assertRaises(OSError):
            asyncio.run(run())

    def test_a_port_already_taken_is_reported_not_raised(self):
        taken = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        taken.bind(("127.0.0.1", 0))
        self.addCleanup(taken.close)
        ini = os.path.join(tempfile.mkdtemp(prefix="grfs-test-"), "grfs.ini")
        self.addCleanup(shutil.rmtree, os.path.dirname(ini), True)
        with open(ini, "w", encoding="utf-8") as f:
            f.write("[server]\nlog_file =\n")
        root = logging.getLogger("grfs")
        saved = root.handlers[:], root.level, root.propagate
        self.addCleanup(self._restore, saved)
        with contextlib.redirect_stderr(io.StringIO()) as console:
            code = main(["--config", ini, "--bind", "127.0.0.1", "--log-level", "error",
                         "--auth-port", str(taken.getsockname()[1]), "--panel-port", "0"])
        self.assertIn("cannot start", console.getvalue())
        self.assertEqual(code, 1)
        self.assertTrue(any("cannot start" in line["text"] for line in logs.ring.lines))

    def test_bad_arguments_stop_before_anything_starts(self):
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            main(["--auth-port", "not-a-number"])

    @staticmethod
    def _restore(saved):
        root = logging.getLogger("grfs")
        root.handlers[:] = saved[0]
        root.setLevel(saved[1])
        root.propagate = saved[2]
