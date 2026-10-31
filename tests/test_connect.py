"""GRFS-Connect.exe (tools/connect, built by tools/make_connect.py), run against a real server.

The exe is built once for the module, offering 127.0.0.1. It runs with _CLUSTER_NETWORK_NAME_ set
the way it sets it for the game, so that the address it works out for UDP 2346 is 127.0.0.1 on
any machine. --check changes nothing, and the hosts steps run on a file of the test's own: the
steps that need administrator rights are not run here.
"""

import os
import shutil
import socket
import stat
import subprocess
import tempfile
import unittest

from tests.live import LiveTest
from tests.test_tools import load

make_connect = load("make_connect")
CSC = make_connect.find_csc() if os.name == "nt" else None
NEEDS_CSC = "needs Windows and its .NET Framework 4 C# compiler"
MARKER = "# GRFSConnect - remove this block to go back to the real Ubisoft servers"
FOLDER = EXE = None


def setUpModule():
    global FOLDER, EXE
    if CSC is None:
        return
    FOLDER = tempfile.mkdtemp(prefix="grfs-connect-test-")
    EXE = os.path.join(FOLDER, "GRFS-Connect.exe")
    make_connect.build(EXE, "127.0.0.1", CSC)


def tearDownModule():
    if FOLDER:
        shutil.rmtree(FOLDER, ignore_errors=True)


def run(*args):
    env = dict(os.environ, _CLUSTER_NETWORK_NAME_="127.0.0.1")
    result = subprocess.run([EXE] + [str(a) for a in args], stdin=subprocess.DEVNULL,
                            capture_output=True, env=env, timeout=120)
    return result.returncode, result.stdout.decode("utf-8")


def closed_udp_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class Builder(unittest.TestCase):
    def test_lan_addresses_come_best_first_and_vpns_stay_out(self):
        self.assertEqual(make_connect.lan_addresses(
            ["100.102.251.59", "172.30.160.1", "127.0.0.1", "10.0.0.5", "169.254.1.2",
             "192.168.1.11", "8.8.8.8", "192.168.1.11"]),
            ["192.168.1.11", "10.0.0.5", "172.30.160.1"])

    @unittest.skipUnless(CSC, NEEDS_CSC)
    def test_a_bad_address_is_refused_before_the_compiler(self):
        with self.assertRaises(ValueError):
            make_connect.build(os.path.join(FOLDER, "never.exe"), "192.168.1", CSC)


@unittest.skipUnless(CSC, NEEDS_CSC)
class Check(LiveTest):
    def check(self, secure_port=None, http_port=None):
        ports = self.server.ports
        return run("--check", "127.0.0.1", "--http-port", http_port or ports["onlineconfig"],
                   "--secure-port", secure_port or ports["secure"])

    def test_a_running_server_passes(self):
        code, out = self.check()
        self.assertEqual(code, 0, out)
        ports = self.server.ports
        self.assertIn("игра пойдёт на 127.0.0.1:%d" % ports["auth"], out)
        self.assertIn("UDP %d (вход) отвечает" % ports["auth"], out)
        self.assertIn("UDP %d (защищённое соединение) отвечает; сервер видит этот компьютер как"
                      " 127.0.0.1:" % ports["secure"], out)
        self.assertIn("Определитель типа NAT отвечает (UDP %d)" % ports["punch"], out)
        self.assertIn("Сервер на этом же компьютере. Игра откроет UDP 2346 на 127.0.0.1", out)
        self.assertIn("Проверка пройдена.", out)
        self.assertNotIn("Подключение", out)          # a look, not a change

    def test_a_server_that_is_not_there_is_a_failure(self):
        port = self.server.ports["onlineconfig"]
        self.server.stop()
        code, out = self.check(http_port=port)
        self.assertEqual(code, 1, out)
        self.assertIn("Сервер не отвечает на 127.0.0.1:%d (TCP)" % port, out)
        self.assertNotIn("UDP", out.split("(TCP)")[0])

    def test_something_else_on_the_port_is_not_taken_for_the_server(self):
        code, out = self.check(http_port=self.server.ports["panel"])
        self.assertEqual(code, 1, out)
        self.assertIn("но не сервер GRFS", out)

    def test_a_silent_udp_port_is_a_failure_with_the_firewall_hint(self):
        port = closed_udp_port()
        code, out = self.check(secure_port=port)
        self.assertEqual(code, 1, out)
        self.assertIn("UDP %d (защищённое соединение) не отвечает: ответа нет за 3 с" % port, out)
        self.assertIn("TCP проходит, а UDP нет", out)
        self.assertEqual(out.count("TCP проходит"), 1)
        self.assertIn("Есть проблемы: 1.", out)


@unittest.skipUnless(CSC, NEEDS_CSC)
class HostsFile(unittest.TestCase):
    BEFORE = ("# the system's own comment\r\n"
              "127.0.0.1 localhost\r\n"
              "10.1.1.1   onlineconfigservice.ubi.com\r\n"
              "127.0.0.1 example.test onlineconfigservice.ubi.com # two names\r\n"
              "\r\n" + MARKER + "\r\n"           # the old GRFSConnect's block
              "192.168.1.9 onlineconfigservice.ubi.com\r\n"
              "\r\n")
    KEPT = ("# the system's own comment\r\n"
            "127.0.0.1 localhost\r\n"
            "# (grfs) 10.1.1.1   onlineconfigservice.ubi.com\r\n"
            "127.0.0.1 example.test # two names\r\n")
    CONNECTED = KEPT + "\r\n" + MARKER + "\r\n192.168.1.8 onlineconfigservice.ubi.com\r\n"

    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".hosts")
        os.close(fd)
        self.addCleanup(self.remove)

    def remove(self):
        os.chmod(self.path, stat.S_IWRITE)
        os.unlink(self.path)

    def write(self, data: bytes):
        with open(self.path, "wb") as f:
            f.write(data)

    def read(self) -> bytes:
        with open(self.path, "rb") as f:
            return f.read()

    def hosts(self, *args):
        return run("--hosts-file", self.path, *args)

    def test_connect_replaces_the_old_block_and_disconnect_removes_it(self):
        self.write(self.BEFORE.encode("utf-8"))
        code, out = self.hosts("connect", "192.168.1.8")
        self.assertEqual(code, 0, out)
        self.assertIn("добавлена строка «192.168.1.8 onlineconfigservice.ubi.com»", out)
        self.assertIn("закомментирована", out)
        self.assertEqual(self.read().decode("utf-8"), self.CONNECTED)

        code, out = self.hosts("connect", "192.168.1.8")
        self.assertEqual(code, 0, out)
        self.assertIn("уже ведёт", out)
        self.assertEqual(self.read().decode("utf-8"), self.CONNECTED)

        code, out = self.hosts("disconnect")
        self.assertEqual(code, 0, out)
        self.assertIn("убрана", out)
        # What was switched off on the way in stays off: it was not wanted then either.
        self.assertEqual(self.read().decode("utf-8"), self.KEPT)

        code, out = self.hosts("disconnect")
        self.assertIn("строки GRFS Connect там нет", out)

    def test_a_line_that_is_already_right_is_left_alone(self):
        before = b"127.0.0.1 localhost\r\n192.168.1.8\tonlineconfigservice.ubi.com\r\n"
        self.write(before)
        code, out = self.hosts("connect", "192.168.1.8")
        self.assertEqual(code, 0, out)
        self.assertIn("уже ведёт", out)
        self.assertEqual(self.read(), before)
        # Not ours to take away.
        code, out = self.hosts("disconnect")
        self.assertEqual(self.read(), before)

    def test_the_encoding_and_the_read_only_flag_are_kept(self):
        # Not UTF-8, so the ANSI code page, whichever it is: the bytes must come back as they were.
        ansi = b"# \xe9\xf2\xee\r\n127.0.0.1 localhost\r\n"
        self.write(ansi)
        os.chmod(self.path, stat.S_IREAD)
        code, out = self.hosts("connect", "192.168.1.8")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.read(), ansi + b"\r\n" + MARKER.encode() +
                         b"\r\n192.168.1.8 onlineconfigservice.ubi.com\r\n")
        self.assertFalse(os.access(self.path, os.W_OK))

        os.chmod(self.path, stat.S_IWRITE)
        self.write(b"\xef\xbb\xbf127.0.0.1 localhost\r\n")
        code, out = self.hosts("connect", "192.168.1.8")
        self.assertEqual(code, 0, out)
        self.assertTrue(self.read().startswith(b"\xef\xbb\xbf127.0.0.1 localhost\r\n\r\n# GRFS"))

    def test_the_command_line_is_checked(self):
        code, out = run("--hosts-file", self.path, "connect", "not-an-address")
        self.assertEqual(code, 2, out)
        code, out = run("--bogus")
        self.assertEqual(code, 2, out)
        self.assertIn("Неизвестный ключ: --bogus", out)


if __name__ == "__main__":
    unittest.main()
