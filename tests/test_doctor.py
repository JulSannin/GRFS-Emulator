"""tools/doctor.py and tools/make_bundle.py: the checks a second player runs, against a real
server, and the archive they arrive in."""

import contextlib
import io
import os
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import unittest
import zipfile

from tests.live import LiveTest
from tests.test_tools import load

doctor = load("doctor")
bundle = load("make_bundle")
launcher = load("launch_game")
DOCTOR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "tools", "doctor.py")


def run_doctor(*args):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = doctor.main(list(args) + ["--report", "", "--no-colour"])
    return code, out.getvalue()


class Check(LiveTest):
    def args(self, *more):
        return ["--host", "127.0.0.1", "--config-port", str(self.server.ports["onlineconfig"]),
                "--p2p-port", "0", "--game-ip", "127.0.0.1", "--detect-timeout", "1"] + list(more)

    def test_a_working_server_passes_every_step(self):
        code, text = run_doctor(*self.args())
        self.assertEqual(code, 0, text)
        for expected in ("SandboxUrl = prudp:/address=127.0.0.1", "вошёл как",
                         "сервер видит этот компьютер как 127.0.0.1", "Open", "всё в порядке"):
            self.assertIn(expected, text)

    def test_a_stopped_server_is_a_failure_that_says_where(self):
        self.server.stop()
        code, text = run_doctor(*self.args())
        self.assertEqual(code, 1)
        self.assertIn("не отвечает", text)
        self.assertNotIn("вошёл как", text)

    def test_the_report_file_has_what_the_screen_had(self):
        path = os.path.join(tempfile.mkdtemp(), "report.txt")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            doctor.main(self.args("--report", path, "--no-colour"))
        with open(path, encoding="utf-8") as f:
            saved = f.read()
        self.assertIn("SandboxUrl", saved)
        self.assertIn("всё в порядке", saved)
        self.assertNotIn(doctor.Player.PASSWORD, saved)


class PeerToPeer(LiveTest):
    def p2p(self, name, peer, wait=15, seconds=6):
        # A process per side: redirect_stdout swaps the one sys.stdout every thread shares.
        result = subprocess.run(
            [sys.executable, DOCTOR, "p2p", "--host", "127.0.0.1", "--config-port",
             str(self.server.ports["onlineconfig"]), "--name", name, "--peer", peer,
             "--p2p-port", "0", "--game-ip", "127.0.0.1", "--wait", str(wait),
             "--time", str(seconds), "--report", "",
             "--no-colour"],
            capture_output=True, timeout=90, env=dict(os.environ, PYTHONIOENCODING="utf-8"))
        return result.returncode, result.stdout.decode("utf-8", "replace")

    def test_two_players_reach_each_other(self):
        results = {}

        def side(name, peer):
            results[name] = self.p2p(name, peer)
        threads = [threading.Thread(target=side, args=pair)
                   for pair in (("alice", "bob"), ("bob", "alice"))]
        for t in threads:
            t.start()
        for t in threads:
            t.join(60)
        for name in ("alice", "bob"):
            code, text = results[name]
            self.assertEqual(code, 0, text)
            self.assertIn("работает в обе стороны", text)

    def test_a_peer_that_never_comes_is_named(self):
        code, text = self.p2p("alice", "nobody-here", wait=2)
        self.assertEqual(code, 1)
        self.assertIn("nobody-here так и не появился", text)


class Exchange(unittest.TestCase):
    """The packet exchange on its own, with the network made to fail one way."""

    def sockets(self):
        a = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        b = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        for s in (a, b):
            s.bind(("127.0.0.1", 0))
            self.addCleanup(s.close)
        return a, b

    def test_one_way_is_reported_against_the_side_that_does_not_receive(self):
        a, b = self.sockets()
        nowhere = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        nowhere.bind(("127.0.0.1", 0))
        dead = nowhere.getsockname()
        nowhere.close()
        result = {}
        # bob aims at alice correctly; alice aims at an address where bob is not.
        t = threading.Thread(target=lambda: result.setdefault(
            "bob", doctor.exchange(b, "bob", [a.getsockname()], 2)))
        t.start()
        heard, acked = doctor.exchange(a, "alice", [dead], 2)
        t.join(10)
        self.assertEqual(heard, b.getsockname())
        self.assertIsNone(acked)
        report = doctor.Report(None, colour=False)
        with contextlib.redirect_stdout(io.StringIO()):
            ok = doctor.verdict(report, "alice", "bob", heard, acked, 2346)
        self.assertFalse(ok)
        self.assertIn("закрыт у bob", "\n".join(report.lines))

    def test_nothing_either_way(self):
        report = doctor.Report(None, colour=False)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertFalse(doctor.verdict(report, "a", "b", None, None, 2346))
        self.assertIn("ни в одну сторону", "\n".join(report.lines))
        self.assertIn("netsh advfirewall", "\n".join(report.lines))


class Responder:
    """A Storm detect server on P and P+1 that can be told to lie, to drive the client's FSM
    down each of its paths."""

    def __init__(self, cmd3_type=3, next_port_shift=0, mapped_ip=None):
        self.cmd3_type = cmd3_type
        self.shift = next_port_shift
        self.mapped_ip = mapped_ip
        self.socks = []
        for _ in range(20):
            first = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            first.bind(("127.0.0.1", 0))
            second = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                second.bind(("127.0.0.1", first.getsockname()[1] + 1))
            except OSError:
                first.close()
                second.close()
                continue
            self.socks = [first, second]
            break
        self.port = self.socks[0].getsockname()[1]
        self.stop = threading.Event()
        self.threads = [threading.Thread(target=self.serve, args=(s, n), daemon=True)
                        for n, s in enumerate(self.socks)]
        for t in self.threads:
            t.start()

    def serve(self, sock, which):
        sock.settimeout(0.05)
        while not self.stop.is_set():
            try:
                data, source = sock.recvfrom(64)
            except (socket.timeout, ConnectionResetError):
                continue
            except OSError:
                return
            cmd = struct.unpack_from(">H", data, 2)[0]
            kind = self.cmd3_type if cmd == 3 else cmd
            port = source[1] + (self.shift if which == 1 else 0)
            ip = self.mapped_ip or source[0]
            sock.sendto(struct.pack(">HH", 19, kind) + data[4:13]
                        + socket.inet_aton(ip)[::-1] + struct.pack(">H", port), source)

    def close(self):
        self.stop.set()
        for s in self.socks:
            s.close()


class NatType(unittest.TestCase):
    def detect(self, local_ip="10.255.0.1", **lies):
        server = Responder(**lies)
        self.addCleanup(server.close)
        target = ("127.0.0.1", server.port)
        return doctor.NatTypeProbe(target, target, timeout=0.5, local_ip=local_ip).run()

    def test_a_server_that_answers_every_question_makes_open(self):
        self.assertEqual(self.detect()[0], "Open")

    def test_cmd3_answered_as_a_plain_reply_is_moderate(self):
        self.assertEqual(self.detect(cmd3_type=1)[0], "Moderate")

    def test_a_different_port_per_destination_is_strict(self):
        self.assertEqual(self.detect(next_port_shift=7)[0], "Strict")

    def test_no_nat_is_open_after_one_question(self):
        kind, why = self.detect(local_ip="127.0.0.1")
        self.assertEqual(kind, "Open")
        self.assertIn("NAT нет", why)

    def test_nobody_there_is_unknown(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.bind(("127.0.0.1", 0))
        target = s.getsockname()
        s.close()
        self.assertEqual(doctor.NatTypeProbe(target, target, timeout=0.2).run()[0], "Unknown")

    def test_replies_the_game_would_drop_are_dropped(self):
        good = struct.pack(">HH", 19, 1) + bytes(9) + socket.inet_aton("1.2.3.4")[::-1] \
            + struct.pack(">H", 5000)
        self.assertEqual(doctor.parse_reply(good), (1, "1.2.3.4", 5000))
        self.assertIsNone(doctor.parse_reply(good[:18]))
        self.assertIsNone(doctor.parse_reply(good + b"\x00"))
        self.assertIsNone(doctor.parse_reply(struct.pack(">HH", 19, 9) + good[4:]))

    def test_one_detect_value_is_a_warning_not_a_failure(self):
        report = doctor.Report(None, colour=False)
        with contextlib.redirect_stdout(io.StringIO()):
            kind = doctor.check_nat_type(report, {"punch_DetectUrls": ["1.2.3.4:5"]})
        self.assertEqual((kind, report.failures, report.warnings), ("Unknown", 0, 1))


class GameAddress(unittest.TestCase):
    """Where the game's UDP 2346 sits, which is not where a socket bound to 0.0.0.0 sits."""

    def check(self, ip, host="127.0.0.1"):
        report = doctor.Report(None, colour=False)
        with contextlib.redirect_stdout(io.StringIO()):
            ok = doctor.check_game_address(report, host, 21031, ip, "pc")
        return ok, report

    def test_an_address_that_reaches_the_server_passes(self):
        ok, report = self.check("127.0.0.1")
        self.assertTrue(ok)
        self.assertIn("игра сядет на 127.0.0.1:2346", "\n".join(report.lines))

    def test_an_address_with_no_way_to_the_server_says_what_to_put_in_hosts(self):
        ok, report = self.check("192.0.2.1")         # not this machine's: no route from it
        self.assertFalse(ok)
        text = "\n".join(report.lines)
        self.assertIn("NAT-проба игры с порта 2346 не уйдёт", text)
        self.assertIn("в hosts поставь 192.0.2.1 вместо 127.0.0.1", text)

    def test_a_name_that_resolves_to_nothing(self):
        ok, report = self.check(None)
        self.assertFalse(ok)
        self.assertEqual(report.failures, 1)

    @unittest.skipUnless(os.name == "nt", "the variable is Windows'")
    def test_the_cluster_variable_moves_the_game(self):
        code = "from grfs.fakegame import game_address; print(game_address()[1])"
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        out = subprocess.run([sys.executable, "-c", code], cwd=root, capture_output=True,
                             text=True, env=dict(os.environ, _CLUSTER_NETWORK_NAME_="127.0.0.2"))
        self.assertEqual(out.stdout.strip(), "127.0.0.2")

    def test_two_copies_on_two_loopback_addresses_hold_one_port_at_once(self):
        one = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        two = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.addCleanup(one.close)
        self.addCleanup(two.close)
        try:
            one.bind(("127.0.0.1", 0))
            port = one.getsockname()[1]
            two.bind(("127.0.0.2", port))
        except OSError as e:
            self.skipTest("no 127.0.0.2 here: %s" % e)
        two.settimeout(1)
        one.sendto(b"x", ("127.0.0.2", port))
        self.assertEqual(two.recvfrom(10), (b"x", ("127.0.0.1", port)))


class LaunchGame(unittest.TestCase):
    def game(self):
        folder = os.path.join(tempfile.mkdtemp(), "steamapps", "common", launcher.GAME_FOLDER)
        os.makedirs(folder)
        open(os.path.join(folder, launcher.LAUNCHER), "wb").close()
        return folder

    def run_main(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = launcher.main(list(args))
        return code, out.getvalue()

    def test_the_game_is_found_in_a_steam_library(self):
        folder = self.game()
        library = folder.split(os.sep + "steamapps" + os.sep)[0]
        self.assertEqual(launcher.find_game([tempfile.mkdtemp(), library]), folder)
        self.assertIsNone(launcher.find_game([tempfile.mkdtemp()]))

    def test_library_folders_are_read_from_steams_list(self):
        path = os.path.join(tempfile.mkdtemp(), "libraryfolders.vdf")
        with open(path, "w", encoding="utf-8") as f:
            f.write('"libraryfolders"\n{\n\t"0"\n\t{\n\t\t"path"\t\t"C:\\\\Program Files (x86)'
                    '\\\\Steam"\n\t}\n\t"1"\n\t{\n\t\t"path"\t\t"D:\\\\SteamLibrary"\n\t}\n}\n')
        self.assertEqual(launcher.libraries_in(path),
                         [os.path.normpath(r"C:\Program Files (x86)\Steam"),
                          os.path.normpath(r"D:\SteamLibrary")])

    def test_the_address_goes_in_the_environment_and_nothing_else_changes(self):
        argv, cwd, env = launcher.command("G", "Future Soldier.exe", "127.0.0.2", ["-x"])
        self.assertEqual(argv, [os.path.join("G", "Future Soldier.exe"), "-x"])
        self.assertEqual((cwd, env["_CLUSTER_NETWORK_NAME_"]), ("G", "127.0.0.2"))
        self.assertEqual(env["PATH"], os.environ["PATH"])

    def test_a_dry_run_starts_nothing(self):
        code, text = self.run_main("--ip", "127.0.0.3", "--game", self.game(), "--dry-run")
        self.assertEqual(code, 0, text)
        self.assertIn("P2P на 127.0.0.3:2346", text)
        self.assertIn("twin_logins = yes", text)

    def test_an_address_whose_port_is_taken_is_refused(self):
        held = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.addCleanup(held.close)
        try:
            held.bind(("127.0.0.4", launcher.P2P_PORT))
        except OSError:
            self.skipTest("cannot hold 127.0.0.4:2346 here")
        code, text = self.run_main("--ip", "127.0.0.4", "--game", self.game(), "--dry-run")
        self.assertEqual(code, 1)
        self.assertIn("127.0.0.5", text)

    def test_no_game_no_start(self):
        code, text = self.run_main("--ip", "127.0.0.2", "--game", tempfile.mkdtemp(),
                                   "--dry-run")
        self.assertEqual(code, 1)
        self.assertIn("игра не найдена", text)

    def test_not_an_address(self):
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            launcher.main(["--ip", "pc-two", "--dry-run"])


class HostsFile(unittest.TestCase):
    def hosts(self, text):
        path = os.path.join(tempfile.mkdtemp(), "hosts")
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return path

    def test_entries_ignore_comments_and_case(self):
        path = self.hosts("# 1.1.1.1 onlineconfigservice.ubi.com\n"
                          "192.168.1.8  OnlineConfigService.ubi.com  # the server\n"
                          "10.0.0.1 other.example onlineconfigservice.ubi.com\n")
        self.assertEqual(doctor.hosts_entries(path), ["192.168.1.8", "10.0.0.1"])

    def test_no_entry_is_a_failure_with_the_line_to_add(self):
        report = doctor.Report(None, colour=False)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertIsNone(doctor.check_hosts(report, self.hosts("127.0.0.1 localhost\n")))
        self.assertEqual(report.failures, 1)
        self.assertIn("onlineconfigservice.ubi.com", "\n".join(report.lines))

    def test_an_entry_the_resolver_does_not_follow_is_caught(self):
        report = doctor.Report(None, colour=False)
        real = socket.gethostbyname
        socket.gethostbyname = lambda name: "93.184.216.34"
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                found = doctor.check_hosts(report, self.hosts(
                    "192.168.1.8 onlineconfigservice.ubi.com\n"))
        finally:
            socket.gethostbyname = real
        self.assertIsNone(found)
        self.assertIn("flushdns", "\n".join(report.lines))

    def test_unreadable_hosts_falls_back_to_the_resolver(self):
        self.assertIsNone(doctor.hosts_entries(os.path.join(tempfile.mkdtemp(), "missing")))


class Bundle(unittest.TestCase):
    def fake_python(self):
        data = io.BytesIO()
        with zipfile.ZipFile(data, "w") as z:
            z.writestr("python.exe", b"MZ")
            z.writestr("python311.zip", b"")
            z.writestr(bundle.PTH_NAME, "python311.zip\n.\n\n# Uncomment to run site.main()"
                                        " automatically\n#import site\n")
        return data.getvalue()

    def test_the_bundle_carries_the_tools_and_nobodys_data(self):
        out = os.path.join(tempfile.mkdtemp(), "GRFS-Tools.zip")
        names = bundle.build(self.fake_python(), out)
        for expected in ("python/python.exe", "app/grfs/__init__.py", "app/grfs/__main__.py",
                         "app/schema/grfs-ddl.json", "app/tools/doctor.py",
                         "app/tools/fake_game.py", "doctor.bat", "p2p-test.bat",
                         "fake-game.bat", "server.bat", "README.txt"):
            self.assertIn(expected, names)
        for name in names:
            self.assertNotIn("__pycache__", name)
            self.assertFalse(name.startswith(("app/data", "app/tests", "app/logs")), name)
        with zipfile.ZipFile(out) as z:
            pth = z.read("GRFS-Tools/python/" + bundle.PTH_NAME).decode("ascii")
            self.assertEqual(pth.splitlines()[:3], ["python311.zip", ".", r"..\app"])
            self.assertNotIn("\nimport site", pth)
            for bat in ("doctor.bat", "p2p-test.bat", "fake-game.bat", "server.bat"):
                text = z.read("GRFS-Tools/" + bat)
                text.decode("ascii")                       # cmd.exe reads these in the OEM page
                self.assertIn(b"\r\n", text)
                self.assertIn(b"pause", text)

    def test_the_path_file_is_patched_once(self):
        once = bundle.pth_text("python311.zip\n.\n")
        self.assertEqual(bundle.pth_text(once), once)

    def test_a_python_that_is_not_the_pinned_one_is_refused(self):
        with self.assertRaises(ValueError):
            bundle.check_python(b"something else")
        with self.assertRaises(ValueError):
            bundle.build(b"PK\x05\x06" + bytes(18), os.path.join(tempfile.mkdtemp(), "x.zip"))


if __name__ == "__main__":
    unittest.main()
