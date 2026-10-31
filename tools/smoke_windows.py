"""Check the distributed Windows executables without changing hosts or the firewall.

    python tools/smoke_windows.py --bundle dist/GRFS-Windows-x64.zip

The archive is extracted into a temporary directory. The real server executable runs on free
loopback ports; the client only runs --check. Profiles are read back after a server restart.
"""

import argparse
import contextlib
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import zipfile

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from grfs.fakegame import Player, fetch_config  # noqa: E402
from grfs.stats import SET                     # noqa: E402


def udp_pair(exclude=()):
    for _ in range(100):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as first:
            first.bind(("127.0.0.1", 0))
            port = first.getsockname()[1]
            if port == 65535 or port in exclude or port + 1 in exclude:
                continue
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as second:
                try:
                    second.bind(("127.0.0.1", port + 1))
                except OSError:
                    continue
                return port, port + 1
    raise OSError("cannot allocate two adjacent UDP ports")


def stop(process):
    if process is not None and process.poll() is None:
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        process.wait(timeout=10)


def verify(bundle: Path, runtime_only: bool = False):
    with tempfile.TemporaryDirectory(prefix="GRFS portable ") as folder:
        root = Path(folder)
        with zipfile.ZipFile(bundle) as archive:
            expected = {"GRFS-Server.exe", "GRFS-Client.exe", "grfs.ini.example", "WINDOWS.md"}
            names = set(archive.namelist())
            if not expected.issubset(names) or archive.testzip():
                raise ValueError("unexpected or corrupted distribution archive")
            if any(name not in expected and not name.startswith(("app/", "python/")) for name in names):
                raise ValueError("unexpected files in distribution archive")
            if any(".." in Path(name).parts or "\\" in name or ":" in name for name in names):
                raise ValueError("unsafe path in distribution archive")
            archive.extractall(root)
        if not runtime_only:
            help_result = subprocess.run([str(root / "GRFS-Client.exe"), "--help"],
                                         capture_output=True, check=True, timeout=10)
            assert "GRFS-Client.exe --check" in help_result.stdout.decode("utf-8")
        # All runtime files come from the distribution, and not from this checkout or its cwd.
        cwd = root / "unrelated working directory"
        cwd.mkdir()
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            http = sock.getsockname()[1]
        auth, secure = udp_pair()
        punch, _ = udp_pair((auth, secure))
        (root / "grfs.ini").write_text(
            "[server]\nbind=127.0.0.1\nonlineconfig_port=%d\nauth_port=%d\nsecure_port=%d\n"
            "punch_port=%d\npanel_port=0\nlog_file=logs/grfs.log\n"
            % (http, auth, secure, punch), encoding="utf-8")
        process = None
        first_pid = None
        try:
            with (root / "console.txt").open("w", encoding="utf-8") as console:
                for restart in (False, True):
                    arguments = ([str(root / "python/python.exe"), "-X", "utf8", "-m", "grfs"]
                                 if runtime_only else [str(root / "GRFS-Server.exe")])
                    if restart:
                        arguments += ["--config", str(root / "grfs.ini")]
                    process = subprocess.Popen(arguments, cwd=cwd,
                                               stdout=console, stderr=subprocess.STDOUT,
                                               env=dict(os.environ, GRFS_RUNTIME_DIR=str(root)))
                    deadline = time.monotonic() + 30
                    while time.monotonic() < deadline:
                        if process.poll() is not None:
                            raise RuntimeError((root / "console.txt").read_text(encoding="utf-8"))
                        try:
                            fetch_config("127.0.0.1", http)
                            break
                        except OSError:
                            time.sleep(0.1)
                    else:
                        raise TimeoutError("packaged server did not start")
                    with contextlib.ExitStack() as players:
                        alice = players.enter_context(Player("127.0.0.1", auth, secure, "bundle-alice"))
                        bob = players.enter_context(Player("127.0.0.1", auth, secure, "bundle-bob"))
                        alice.login()
                        bob.login()
                        query = {"queries": [{"board": 1, "contexts": [], "statIds": [1]}],
                                 "pids": [alice.pid]}
                        before = alice.call("PlayerStatisticsProtocol", "ReadPlayerStatsInt32", query)
                        if restart:
                            assert alice.pid == first_pid
                            assert before["results"][0]["rows"][0]["stats"] == [{"statId": 1, "value": 2500}]
                            print("Account IDs and statistics survive restart: OK", flush=True)
                        else:
                            first_pid = alice.pid
                            assert before == {"results": [{"pid": alice.pid, "rows": []}]}
                            alice.call("PlayerStatisticsProtocol", "SendPlayerStatsInt32", {"statGroups": [
                                {"board": 1, "contexts": [], "stats": [{"statId": 1, "value": 2500, "op": SET}]}]})
                            assert alice.call("PlayerStatisticsProtocol", "ReadPlayerStatsInt32", query)[
                                "results"][0]["rows"][0]["stats"] == [{"statId": 1, "value": 2500}]
                            print("Profile reads return success; writes can be read back: OK", flush=True)
                        maps = alice.call("MapListProtocol", "GetMapList", {"categoryId": 0x4010})
                        assert maps["mapIds"]
                        lookup = {"gameSessionTypeID": 1, "participantIDs": [alice.pid]}
                        assert bob.call("GameSessionProtocol", "SearchSessionsWithParticipants", lookup) == {
                            "searchResults": []}
                        key = alice.call("GameSessionProtocol", "CreateSession", {"gameSession": {
                            "m_typeID": 1, "m_attributes": [{"m_ID": 3, "m_value": 4},
                                                             {"m_ID": 4, "m_value": 0}]}})["gameSessionKey"]
                        alice.call("GameSessionProtocol", "AddParticipants", {
                            "gameSessionKey": key, "publicParticipantIDs": [alice.pid, bob.pid],
                            "privateParticipantIDs": []})
                        found = bob.call("GameSessionProtocol", "SearchSessionsWithParticipants", lookup)
                        assert found["searchResults"][0]["m_sessionKey"] == key
                        print("Bundled map table and session creation/search/participants: OK", flush=True)
                    if not restart and not runtime_only:
                        result = subprocess.run([
                            str(root / "GRFS-Client.exe"), "--check", "127.0.0.1", "--http-port", str(http),
                            "--secure-port", str(secure)], cwd=cwd, capture_output=True, timeout=30,
                            env=dict(os.environ, _CLUSTER_NETWORK_NAME_="127.0.0.1"))
                        if result.returncode:
                            raise RuntimeError(result.stdout.decode("utf-8", "replace"))
                        assert "Проверка пройдена." in result.stdout.decode("utf-8")
                        print("Native client checks HTTP, PRUDP ports and NAT detector: OK", flush=True)
                    stop(process)
                    process = None
        finally:
            stop(process)
        assert (root / "data/stats.json").is_file()
        assert not (cwd / "data").exists()
        log = (root / "logs/grfs.log").read_text(encoding="utf-8")
        assert not any(word in log for word in (" ERROR ", "Traceback", "no service")), log
        print("Portable data/config/log paths and server log: OK", flush=True)
        if runtime_only:
            print("Native .exe execution skipped (--runtime-only); checked the shipped Python and server.", flush=True)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, default=ROOT / "dist/GRFS-Windows-x64.zip")
    parser.add_argument("--runtime-only", action="store_true",
                        help="check the shipped signed Python and server without executing native launchers")
    args = parser.parse_args(argv)
    if sys.platform != "win32":
        parser.error("run with Windows Python")
    verify(args.bundle.resolve(), args.runtime_only)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
