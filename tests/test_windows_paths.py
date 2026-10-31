"""Packaged servers keep player data independently of their interpreter and resource paths."""

import contextlib
import io
import logging
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from grfs import log, protocols, stats
from grfs.backend import Backend
from grfs.config import Config
from grfs.maps import Maps


class PortableServer(unittest.TestCase):
    def test_native_launcher_separates_shipped_runtime_and_persistent_files(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "grfs.ini").write_text("[server]\nauth_port=31030\n", encoding="utf-8")
            with patch.dict(os.environ, {"GRFS_RUNTIME_DIR": str(root)}, clear=True):
                backend = Backend()
                self.assertEqual(backend.auth_port, 31030)
                self.assertEqual(Path(backend.data_dir), root / "data")
                self.assertTrue(Maps().keys_for(0x4010))

    def test_config_and_profile_survive_restarting_from_another_working_directory(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "grfs.ini").write_text("[server]\ndata_dir = profiles\nauth_port = 31030\n",
                                          encoding="utf-8")
            with patch.object(sys, "frozen", True, create=True), \
                    patch.object(sys, "executable", str(root / "GRFS-Server.exe")), \
                    patch.dict(os.environ, {}, clear=True):
                first = Backend()
                self.assertEqual(first.auth_port, 31030)
                self.assertEqual(Path(first.data_dir), root / "profiles")
                pid = first.accounts.pid_for("portable-player")
                first.router.service_for(protocols.PLAYER_STATISTICS).stats.update(
                    "int32", pid, 1, (), 1, 2500, stats.SET)
                first.save()
                second = Backend()
                self.assertEqual(second.accounts.pid_for("portable-player"), pid)
                self.assertEqual(second.router.service_for(protocols.PLAYER_STATISTICS).stats.get(
                    "int32", pid, 1, (), 1), 2500)
                self.assertTrue((root / "profiles/stats.json").is_file())
                self.assertTrue(Maps().keys_for(0x4010))

    def test_an_absolute_data_directory_is_respected(self):
        with tempfile.TemporaryDirectory() as folder:
            chosen = Path(folder) / "custom"
            config = Config(path=os.devnull, overrides={"data_dir": str(chosen)})
            backend = Backend(config)
            self.assertEqual(Path(backend.data_dir), chosen)

    def test_logs_live_beside_the_executable(self):
        logger = logging.getLogger("grfs")
        previous = (list(logger.handlers), logger.level, logger.propagate)
        with tempfile.TemporaryDirectory() as folder:
            try:
                with patch.object(sys, "frozen", True, create=True), \
                        patch.object(sys, "executable", str(Path(folder) / "GRFS-Server.exe")):
                    with contextlib.redirect_stderr(io.StringIO()):
                        log.setup("info", "logs/grfs.log")
                        log.get("portable-test").info("portable log check")
                    path = Path(folder) / "logs/grfs.log"
                    self.assertIn("portable log check", path.read_text(encoding="utf-8"))
            finally:
                for handler in logger.handlers:
                    if handler not in previous[0]:
                        handler.close()
                logger.handlers = previous[0]
                logger.setLevel(previous[1])
                logger.propagate = previous[2]


if __name__ == "__main__":
    unittest.main()
