"""tools/rig.py: reading how far each copy of the game got from the server's log, the checks
before a run, a real server started and stopped the way the rig does it, and - only when asked
for with GRFS_DESKTOP_TESTS=1, because they take the keyboard and the mouse - grfs.desktop
driving a stand-in window."""

import contextlib
import io
import json
import os
import random
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import warnings

from grfs.fakegame import Player, Wire
from grfs.punch import ANY_PORTS
from tests.test_tools import load

rig = load("rig")
HERE = os.path.dirname(os.path.abspath(__file__))
STANDIN = os.path.join(HERE, "standin_window.py")
WINDOWS = os.name == "nt"

# Lines as the server writes them (log format, then the messages of auth, nat, secure,
# map_list and game_session), for two copies of one account with twin_logins on.
LOG = """\
2026-10-25 18:00:00 INFO  main               ready: onlineconfig=80, auth=21030
2026-10-25 18:00:05 INFO  auth               127.0.0.1 signed in as Wolf (pid 0x3E9) on connection 01; secure server at prudps:/...
2026-10-25 18:00:05 INFO  auth               127.0.0.1 signed in as Tracking (pid 0x2) on connection 02; secure server at prudps:/...
2026-10-25 18:00:06 INFO  secure             Wolf registered 1 station URL(s) as connection 7: prudp:/address=127.0.0.1;port=2346
2026-10-25 18:00:06 INFO  nat                told ('127.0.0.1', 2346) its public address: x (1 answered)
2026-10-25 18:00:09 INFO  auth               127.0.0.2 signed in as Wolf while a game with that name is running - this one is Wolf_2 (twin_logins)
2026-10-25 18:00:09 INFO  auth               127.0.0.2 signed in as Wolf_2 (pid 0x3EA) on connection 01; secure server at prudps:/...
2026-10-25 18:00:10 INFO  secure             Wolf_2 registered 1 station URL(s) as connection 8: prudp:/address=127.0.0.2;port=2346
2026-10-25 18:00:10 INFO  nat                told ('127.0.0.2', 2346) its public address: x (2 answered)
2026-10-25 18:00:12 INFO  map_list           Wolf may host 12 world(s) for mask 0x1, e.g. Nigeria
2026-10-25 18:00:20 INFO  game_session       Wolf hosts session 5 (type 1): {}
2026-10-25 18:00:21 INFO  game_session       Wolf invited Wolf_2 to session 5
2026-10-25 18:00:25 INFO  game_session       Wolf_2 accepted Wolf's invitation to session 5
2026-10-25 18:00:25 INFO  game_session       Wolf_2 may join session 5
2026-10-25 18:00:26 INFO  game_session       session 5: Wolf added Wolf, Wolf_2 -> public 2/4, private 0/0
2026-10-25 18:00:27 WARN  secure             Wolf_2: something odd
"""

COPIES = [{"n": 1, "ip": "127.0.0.1"}, {"n": 2, "ip": "127.0.0.2"}]


class Names(unittest.TestCase):
    def test_each_copy_by_the_address_it_signed_in_from(self):
        self.assertEqual(rig.assign_names(LOG, COPIES), {1: "Wolf", 2: "Wolf_2"})

    def test_the_telemetry_login_is_nobody(self):
        text = LOG.replace("as Wolf (pid", "as Nobody (pid").replace("as Wolf_2 (pid", "x")
        self.assertNotIn("Tracking", rig.assign_names(text, COPIES).values())

    def test_one_address_for_both_copies_falls_back_to_the_order(self):
        text = LOG.replace("127.0.0.2", "127.0.0.1")
        self.assertEqual(rig.assign_names(text, COPIES), {1: "Wolf", 2: "Wolf_2"})

    def test_a_copy_not_signed_in_yet_has_no_name(self):
        first = LOG.split("127.0.0.2 signed in as Wolf while")[0]
        self.assertEqual(rig.assign_names(first, COPIES), {1: "Wolf", 2: None})
        self.assertEqual(rig.assign_names("", COPIES), {1: None, 2: None})

    def test_a_restart_under_the_same_name_is_still_one_copy(self):
        text = LOG + LOG.splitlines()[1] + "\n"
        self.assertEqual(rig.assign_names(text, COPIES), {1: "Wolf", 2: "Wolf_2"})


class Steps(unittest.TestCase):
    def test_the_host_and_the_joiner(self):
        host = rig.steps_reached(LOG, "127.0.0.1", "Wolf")
        self.assertEqual(set(host), {"signed-in", "registered", "nat-echo", "menu", "hosting",
                                     "in-session", "invited"})
        self.assertEqual(host["hosting"], {"session": "5"})
        self.assertEqual(host["invited"], {"other": "Wolf_2"})
        joiner = rig.steps_reached(LOG, "127.0.0.2", "Wolf_2")
        self.assertEqual(set(joiner), {"signed-in", "registered", "nat-echo", "joining",
                                       "in-session", "accepted"})
        self.assertEqual(joiner["joining"], {"session": "5"})

    def test_one_name_does_not_take_another_names_lines(self):
        # "Wolf" is the start of "Wolf_2": nothing of Wolf_2's may count for Wolf.
        only_twin = "\n".join(line for line in LOG.splitlines()
                              if "Wolf_2" in line and "Wolf added" not in line
                              and "Wolf invited" not in line)
        self.assertEqual(rig.steps_reached(only_twin, "127.0.0.1", "Wolf"), {})

    def test_without_a_name_only_the_address_says_anything(self):
        reached = rig.steps_reached(LOG, "127.0.0.1", None)
        self.assertEqual(set(reached), {"signed-in", "nat-echo"})

    def test_every_step_has_a_pattern_the_server_actually_logs(self):
        # The patterns are only as good as the log lines they look for; each fixed part of
        # each must be in the source of the server.
        sources = ""
        for folder, _, files in os.walk(os.path.join(os.path.dirname(HERE), "grfs")):
            for name in files:
                if name.endswith(".py"):
                    with open(os.path.join(folder, name), encoding="utf-8") as f:
                        sources += f.read()
        fixed = {"signed-in": "signed in as %s (pid", "registered": "registered %d station URL",
                 "nat-echo": "its public address", "menu": "may host %d world",
                 "hosting": "hosts session %d", "joining": "may join session %d",
                 "in-session": "session %d: %s added %s", "invited": "invited %s to session",
                 "accepted": "accepted %s's invitation"}
        self.assertEqual(set(fixed), set(rig.STEP_NAMES))
        for step, text in fixed.items():
            self.assertIn(text, sources, step)


def hosts_file(text):
    f = tempfile.NamedTemporaryFile("w", suffix=".hosts", delete=False, encoding="utf-8")
    f.write(text)
    f.close()
    return f.name


class Preflight(unittest.TestCase):
    def setUp(self):
        self.game = tempfile.mkdtemp()
        self.none_installed = tempfile.mkdtemp()
        self.dirs = rig.UBISOFT_CONNECT_DIRS
        rig.UBISOFT_CONNECT_DIRS = [os.path.join(self.none_installed, "nope")]
        self.addCleanup(setattr, rig, "UBISOFT_CONNECT_DIRS", self.dirs)

    def check(self, hosts, processes=("upc.exe",), ports=None):
        path = hosts_file(hosts)
        self.addCleanup(os.unlink, path)
        return rig.preflight(path, ports=ports or {}, game=self.game, processes=processes)

    def test_all_in_place(self):
        out = self.check("127.0.0.1 onlineconfigservice.ubi.com\n")
        self.assertTrue(all(ok for ok, _, _ in out), out)

    def test_no_hosts_line_says_how_to_add_it(self):
        ok, what, fix = self.check("# 127.0.0.1 onlineconfigservice.ubi.com\n")[0]
        self.assertFalse(ok)
        self.assertIn("setup_once.ps1", fix)

    def test_a_line_for_another_address(self):
        ok, what, _ = self.check("10.0.0.5   onlineconfigservice.ubi.com   # friend\n")[0]
        self.assertFalse(ok)
        self.assertIn("10.0.0.5", what)

    def test_one_of_several_names_on_a_line(self):
        ok, _, _ = self.check("127.0.0.1 localhost ONLINECONFIGSERVICE.UBI.COM")[0]
        self.assertTrue(ok)

    def test_an_unreadable_hosts_file(self):
        out = rig.preflight(os.path.join(self.game, "missing"), ports={}, game=self.game,
                            processes=[])
        self.assertFalse(out[0][0])

    def test_ubisoft_connect_missing_installed_and_running(self):
        hosts = "127.0.0.1 onlineconfigservice.ubi.com\n"
        ok, what, fix = self.check(hosts, processes=[])[2]
        self.assertFalse(ok)
        self.assertIn("не установлен", what)
        rig.UBISOFT_CONNECT_DIRS = [self.none_installed]
        ok, what, _ = self.check(hosts, processes=["explorer.exe"])[2]
        self.assertFalse(ok)
        self.assertIn("не запущен", what)
        self.assertTrue(self.check(hosts, processes=["UbisoftConnect.exe"])[2][0])

    def test_a_taken_port(self):
        taken = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        taken.bind(("0.0.0.0", 0))
        self.addCleanup(taken.close)
        port = taken.getsockname()[1]
        ok, what, _ = self.check("127.0.0.1 onlineconfigservice.ubi.com\n",
                                 ports={"auth_port": port})[3]
        self.assertFalse(ok)
        self.assertIn("UDP %d" % port, what)


SETUP = os.path.join(os.path.dirname(HERE), "tools", "setup_once.ps1")


@unittest.skipUnless(WINDOWS, "setup_once.ps1 is PowerShell for Windows")
class SetupOnce(unittest.TestCase):
    """The hosts part of tools/setup_once.ps1, on a file of its own - with -HostsFile it asks
    for no elevation and touches nothing else."""

    def setup(self, path):
        done = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                               SETUP, "-HostsFile", path], capture_output=True, text=True,
                              timeout=60)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        with open(path, encoding="utf-8", newline="") as f:
            return done.stdout, f.read()

    def test_added_once_whatever_the_file_ended_with(self):
        for start in ("127.0.0.1 localhost", "127.0.0.1 localhost\n", "", "# Кириллица\r\n"):
            path = hosts_file(start)
            self.addCleanup(os.unlink, path)
            said, text = self.setup(path)
            self.assertIn("added", said)
            self.assertTrue(text.startswith(start.rstrip("\r\n")), repr(text))
            self.assertTrue(text.endswith("127.0.0.1 onlineconfigservice.ubi.com\r\n"), repr(text))
            said, again = self.setup(path)
            self.assertIn("already", said)
            self.assertEqual(again, text)
            self.assertTrue(rig.preflight(path, ports={}, game=".", processes=["upc.exe"])[0][0])

    def test_another_address_is_commented_out_not_lost(self):
        path = hosts_file("10.1.1.1   onlineconfigservice.ubi.com\n127.0.0.1 localhost\n")
        self.addCleanup(os.unlink, path)
        _, text = self.setup(path)
        self.assertIn("# (grfs) 10.1.1.1   onlineconfigservice.ubi.com", text)
        self.assertEqual(rig._hosts_entries(path), ["127.0.0.1"])


def free_ports():
    """Ports for every listener of a server, the NAT detector's two side by side."""
    held, out = [], {}

    def grab(kind, port=0):
        s = socket.socket(socket.AF_INET, kind)
        try:
            s.bind(("0.0.0.0", port))
        except OSError:
            s.close()
            raise
        held.append(s)
        return s.getsockname()[1]
    try:
        for key in ("onlineconfig_port", "panel_port"):
            out[key] = grab(socket.SOCK_STREAM)
        for key in ("auth_port", "secure_port"):
            out[key] = grab(socket.SOCK_DGRAM)
        while "punch_port" not in out:
            try:
                # Child startup can consume an ephemeral port after we release it. Use the
                # production detector's range so both consecutive ports stay available.
                port = grab(socket.SOCK_DGRAM, random.randrange(*ANY_PORTS))
                grab(socket.SOCK_DGRAM, port + 1)
                out["punch_port"] = port
            except OSError:
                pass
    finally:
        for s in held:
            s.close()
    return out


def run(*argv):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = rig.main(list(argv))
    return code, out.getvalue()


@unittest.skipUnless(WINDOWS, "the rig is for Windows, where the game is")
class Server(unittest.TestCase):
    """The rig's own server, as a child process, with fake games signing in from 127.0.0.1 and
    127.0.0.2 and sleeping processes standing in for the copies."""

    @classmethod
    def setUpClass(cls):
        # The rig starts the server and the copies to outlive the command that started them.
        quiet = warnings.catch_warnings()
        quiet.__enter__()
        cls.addClassCleanup(quiet.__exit__, None, None, None)
        warnings.filterwarnings("ignore", r"subprocess \d+ is still running", ResourceWarning)
        cls.dir = tempfile.mkdtemp()
        cls.rig = rig.Rig(cls.dir)
        cls.ports = free_ports()
        cls.server = cls.rig.server_up(cls.ports)

    @classmethod
    def tearDownClass(cls):
        rig.Rig(cls.dir).stop_all()

    def test_1_the_server_runs_with_its_own_data_and_twin_logins(self):
        self.assertTrue(rig._alive(self.server["pid"]))
        with open(os.path.join(self.dir, "grfs.ini"), encoding="utf-8") as f:
            ini = f.read()
        self.assertIn("twin_logins = yes", ini)
        self.assertIn("data_dir = %s" % os.path.join(self.dir, "data"), ini)
        self.assertIn("auth_port = %d" % self.ports["auth_port"], ini)
        self.assertIn(" ready: ", self.rig.log_text())
        # A second `up` finds it running and starts nothing.
        self.assertEqual(rig.Rig(self.dir).server_up(self.ports)["pid"], self.server["pid"])

    def test_2_copies_their_names_and_their_steps(self):
        sleeper = [sys.executable, "-c", "import time; time.sleep(60)"]
        code, text = run("--dir", self.dir, "launch", "1", "--command", "|".join(sleeper))
        self.assertEqual(code, 0, text)
        code, text = run("--dir", self.dir, "launch", "2", "--command", "|".join(sleeper))
        self.assertEqual(code, 0, text)
        with open(os.path.join(self.dir, "state.json"), encoding="utf-8") as f:
            state = json.load(f)
        self.assertEqual([c["ip"] for c in state["copies"]], ["127.0.0.1", "127.0.0.2"])

        players = []
        for ip in ("127.0.0.1", "127.0.0.2"):
            player = Player("127.0.0.1", self.ports["auth_port"], self.ports["secure_port"],
                            "RigWolf", wire=Wire(bind=ip))
            players.append(player)
            player.login()
        self.addCleanup(lambda: [(p.close(), p.wire.close()) for p in players])

        deadline = time.monotonic() + 5
        current = rig.Rig(self.dir)
        while time.monotonic() < deadline and None in current.names().values():
            time.sleep(0.1)
        self.assertEqual(current.names(), {1: "RigWolf", 2: "RigWolf_2"})
        self.assertIn("registered", current.progress(1))
        self.assertIn("registered", current.progress(2))

        code, text = run("--dir", self.dir, "wait", "2", "registered", "--timeout", "5")
        self.assertEqual(code, 0, text)
        code, text = run("--dir", self.dir, "wait", "1", "hosting", "--timeout", "1")
        self.assertEqual(code, 1)
        self.assertIn("signed-in, registered", text)

        code, text = run("--dir", self.dir, "status")
        self.assertEqual(code, 0, text)
        self.assertIn("copy 2 (127.0.0.2)", text)
        self.assertIn("name RigWolf_2", text)
        self.assertIn("registered", text)

        code, text = run("--dir", self.dir, "log", "--grep", "RigWolf_2", "--tail", "100")
        self.assertIn("signed in as RigWolf_2", text)
        # The made-up password never reaches the log the rig keeps.
        self.assertNotIn(Player.PASSWORD, current.log_text(0))

    def test_3_a_copy_that_does_not_exist(self):
        with self.assertRaises(SystemExit):
            run("--dir", self.dir, "keys", "9", "enter")

    def test_9_down_stops_everything(self):
        pids = [self.server["pid"]] + [c["pid"] for c in rig.Rig(self.dir).state["copies"]]
        code, text = run("--dir", self.dir, "down")
        self.assertEqual(code, 0, text)
        self.assertIn("server (%d)" % self.server["pid"], text)
        time.sleep(0.5)
        self.assertFalse(any(rig._alive(pid) for pid in pids))
        self.assertEqual(rig.Rig(self.dir).state, {"server": None, "copies": []})
        code, text = run("--dir", self.dir, "down")
        self.assertIn("nothing was running", text)


@unittest.skipUnless(WINDOWS, "the rig is for Windows, where the game is")
class ServerThatCannotStart(unittest.TestCase):
    def test_a_taken_port_is_reported_not_waited_for(self):
        ports = free_ports()
        taken = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        taken.bind(("0.0.0.0", ports["auth_port"]))
        self.addCleanup(taken.close)
        started = time.monotonic()
        with self.assertRaises(SystemExit) as caught:
            rig.Rig(tempfile.mkdtemp()).server_up(ports)
        self.assertLess(time.monotonic() - started, 14)
        self.assertIn("stopped at once", str(caught.exception))


@unittest.skipUnless(WINDOWS, "grfs.desktop is for Windows")
class DesktopQuiet(unittest.TestCase):
    """grfs.desktop where it neither opens a window nor sends a key."""

    def setUp(self):
        from grfs import desktop
        self.desktop = desktop

    def test_png_round_trip(self):
        bgra = bytes([0, 0, 255, 255, 255, 0, 0, 255, 0, 255, 0, 255, 10, 20, 30, 255])
        path = os.path.join(tempfile.mkdtemp(), "x.png")
        self.desktop.write_png(path, 2, 2, bgra)
        self.assertEqual(self.desktop.read_png(path),
                         (2, 2, bytes([255, 0, 0, 0, 0, 255, 0, 255, 0, 30, 20, 10])))

    def test_a_small_drawing_stretched_to_the_window(self):
        # What PrintWindow gives for a DPI-unaware window at 200%: its 2x1 picture in the top
        # left corner of a 4x2 one.
        red, blue, black = b"\0\0\xff\xff", b"\xff\0\0\xff", b"\0\0\0\0"
        drawn = red + blue + black * 2 + black * 4
        self.assertEqual(self.desktop._stretch(drawn, 4, 2, 1, 4, 2),
                         (red * 2 + blue * 2) * 2)
        self.assertEqual(self.desktop._stretch(red + blue, 2, 2, 1, 3, 1), red + red + blue)

    def test_letters_and_digits_whatever_the_layout(self):
        self.assertEqual([self.desktop._vk(k) for k in ("a", "z", "0", "9")],
                         [0x41, 0x5A, 0x30, 0x39])

    def test_a_person_at_the_keyboard_keeps_it(self):
        # Input from the person counts, this process's own does not; with the bar set beyond
        # reach the window is not brought in front and nothing is sent.
        old = self.desktop.IDLE_NEEDED, self.desktop._sent_at
        self.addCleanup(setattr, self.desktop, "IDLE_NEEDED", old[0])
        self.addCleanup(setattr, self.desktop, "_sent_at", old[1])
        self.desktop.IDLE_NEEDED, self.desktop._sent_at = 1e12, None
        window = self.desktop.Window(0)
        window.patience = 0.3
        focused = []
        window.focus = lambda *a: focused.append(1) or True
        with self.assertRaises(self.desktop.DesktopError) as caught:
            self.desktop.wait_idle(patience=0.3)
        self.assertIn("nothing was sent", str(caught.exception))
        with self.assertRaises(self.desktop.DesktopError):
            window.keys(["enter"])
        self.assertEqual(focused, [])


DESKTOP = WINDOWS and os.environ.get("GRFS_DESKTOP_TESTS") == "1"


@unittest.skipUnless(DESKTOP, "takes the keyboard and the mouse: set GRFS_DESKTOP_TESTS=1")
class Desktop(unittest.TestCase):
    """grfs.desktop against tests/standin_window.py: a known picture, and a record of what
    it was sent."""

    def setUp(self):
        from grfs import desktop
        self.desktop = desktop
        desktop.dpi_aware()
        self.dir = tempfile.mkdtemp()
        self.record = os.path.join(self.dir, "record.txt")
        self.process = subprocess.Popen([sys.executable, STANDIN, self.record, "GRFS stand-in"])
        self.addCleanup(self.process.wait)
        self.addCleanup(self.process.kill)
        deadline = time.monotonic() + 10
        self.window = None
        while time.monotonic() < deadline:
            self.window = desktop.main_window(self.process.pid)
            if self.window and "ready" in self.lines():
                break
            time.sleep(0.1)
        self.assertIsNotNone(self.window, "the stand-in has no window")
        time.sleep(0.3)

    def lines(self):
        if not os.path.exists(self.record):
            return []
        with open(self.record, encoding="utf-8") as f:
            return f.read().splitlines()

    def wait_for(self, count, prefix):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            got = [line for line in self.lines() if line.startswith(prefix)]
            if len(got) >= count:
                return got
            time.sleep(0.05)
        return [line for line in self.lines() if line.startswith(prefix)]

    def test_screenshot_shows_the_picture(self):
        path = self.window.screenshot(os.path.join(self.dir, "shot.png"))
        width, height, rgb = self.desktop.read_png(path)
        self.assertEqual((width, height), self.window.rect[2:])
        middle = height // 2 * width * 3
        self.assertEqual(tuple(rgb[middle + 10 * 3:middle + 10 * 3 + 3]), (255, 0, 0))
        right = middle + (width - 10) * 3
        self.assertEqual(tuple(rgb[right:right + 3]), (0, 0, 255))

    def test_keys_arrive_as_keys(self):
        self.window.keys(["a", "enter", "down"], gap=0.1)
        got = self.wait_for(3, "key ")
        codes = [int(line.split()[2]) for line in got]
        self.assertEqual(codes[:3], [0x41, 0x0D, 0x28])

    def test_text_arrives_as_characters(self):
        self.window.type("Hi!")
        time.sleep(0.5)
        # Each character comes as a press with it and a release without.
        typed = [line.split()[-1] for line in self.wait_for(3, "key ") if not line.endswith("''")]
        self.assertEqual(typed, ["'H'", "'i'", "'!'"])

    def test_a_click_lands_where_the_screenshot_says(self):
        width, height = self.window.rect[2:]
        self.window.click(width // 4, height // 2)
        self.window.click(width * 3 // 4, height // 2, button="right")
        got = self.wait_for(2, "click ")
        self.assertEqual(len(got), 2, self.lines())
        (_, b1, x1, y1, _), (_, b2, x2, _, _) = [line.split() for line in got]
        # The stand-in is 320 logical pixels wide, whatever the screen's scale.
        self.assertEqual(b1, "1")
        self.assertAlmostEqual(int(x1), 80, delta=3)
        self.assertAlmostEqual(int(y1), 100, delta=3)
        self.assertEqual(b2, "3")
        self.assertAlmostEqual(int(x2), 240, delta=3)

    def test_nothing_is_sent_to_a_window_that_has_gone(self):
        self.process.kill()
        self.process.wait()
        time.sleep(0.3)
        with self.assertRaises(self.desktop.DesktopError):
            self.window.keys(["enter"])


if __name__ == "__main__":
    unittest.main()
