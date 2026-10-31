"""The extracted Linux distribution, started from another directory and stopped with SIGTERM."""

import os
from pathlib import Path
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest

from grfs.fakegame import Player, fetch_config
from grfs.stats import SET
from tests.test_rig import free_ports
from tests.test_tools import load


@unittest.skipUnless(sys.platform.startswith("linux"), "Linux launcher test")
class LinuxPackage(unittest.TestCase):
    def test_extracted_server_keeps_profiles_and_stops_cleanly_on_sigterm(self):
        with tempfile.TemporaryDirectory(prefix="grfs linux ") as folder:
            temporary = Path(folder)
            archive = temporary / "server.tar.gz"
            load("build_linux").build(temporary / "build", archive)
            with tarfile.open(archive) as bundle:
                names = bundle.getnames()
                self.assertTrue(bundle.getmember("GRFS-Linux/run-server.sh").mode & 0o111)
                self.assertIn("GRFS-Linux/schema/maps.json", names)
                self.assertFalse(any(Path(name).suffix in (".exe", ".dll", ".pyd", ".pyc") for name in names))
                self.assertFalse(any(part in ("data", "logs", "__pycache__")
                                     for name in names for part in Path(name).parts))
                bundle.extractall(temporary / "unpacked", filter="data")
            root = temporary / "unpacked/GRFS-Linux"
            cwd = temporary / "different working directory"
            cwd.mkdir()
            ports = free_ports()
            (root / "grfs.ini").write_text(
                "[server]\nbind=127.0.0.1\n" + "".join("%s=%d\n" % item for item in ports.items()),
                encoding="utf-8")
            env = dict(os.environ, GRFS_PYTHON=sys.executable, GRFS_DATA_DIR="data",
                       GRFS_LOG_FILE="logs/grfs.log", GRFS_RUNTIME_DIR=str(cwd))
            pid = None
            with (temporary / "console.txt").open("w", encoding="utf-8") as console:
                for restart in (False, True):
                    process = subprocess.Popen([str(root / "run-server.sh")], cwd=cwd,
                                               env=env, stdout=console, stderr=subprocess.STDOUT)
                    try:
                        deadline = time.monotonic() + 10
                        while True:
                            if process.poll() is not None:
                                self.fail((temporary / "console.txt").read_text(encoding="utf-8"))
                            try:
                                fetch_config("127.0.0.1", ports["onlineconfig_port"])
                                break
                            except OSError:
                                if time.monotonic() >= deadline:
                                    self.fail("packaged server did not start")
                                time.sleep(0.02)
                        with Player("127.0.0.1", ports["auth_port"], ports["secure_port"], "linux-player") as player:
                            player.login()
                            query = {"queries": [{"board": 1, "contexts": [], "statIds": [1]}],
                                     "pids": [player.pid]}
                            if restart:
                                self.assertEqual(player.pid, pid)
                            else:
                                pid = player.pid
                                player.call("PlayerStatisticsProtocol", "SendPlayerStatsInt32", {"statGroups": [
                                    {"board": 1, "contexts": [], "stats": [{"statId": 1, "value": 2500, "op": SET}]}]})
                            profile = player.call("PlayerStatisticsProtocol", "ReadPlayerStatsInt32", query)
                            self.assertEqual(profile["results"][0]["rows"][0]["stats"],
                                             [{"statId": 1, "value": 2500}])
                            self.assertTrue(player.call("MapListProtocol", "GetMapList", {
                                "categoryId": 0x4010})["mapIds"])
                        process.send_signal(signal.SIGTERM)
                        self.assertEqual(process.wait(timeout=10), 0)
                    finally:
                        if process.poll() is None:
                            process.kill()
                            process.wait(timeout=5)
            self.assertTrue((root / "data/stats.json").is_file())
            self.assertFalse((cwd / "data").exists())
            log = (root / "logs/grfs.log").read_text(encoding="utf-8")
            self.assertEqual(log.count("stopped"), 2)
            self.assertNotIn(" ERROR ", log)


if __name__ == "__main__":
    unittest.main()
