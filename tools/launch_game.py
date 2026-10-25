"""Starts the game with its P2P socket on an address of its own, so that two copies can run on
one machine for a co-op test.

    python tools/launch_game.py --ip 127.0.0.1      the first copy
    python tools/launch_game.py --ip 127.0.0.2      the second

The game binds UDP 2346 on the first address its machine name resolves to (5DF260, 5EBFF0), and
only one process can hold <address>:2346 - which is why a second copy normally has no co-op. The
environment variable _CLUSTER_NETWORK_NAME_, Windows' own for clustered services, replaces the
machine name for one process and everything it starts; set to an address, it resolves to that
address. Every 127.x address is this machine with no setup at all, and copies on 127.0.0.1 and
127.0.0.2 reach each other and a server on 127.0.0.1. Nothing in the game folder is touched.

Both copies get the same account from Ubisoft Connect, so the server needs twin_logins = yes in
grfs.ini: the second copy then plays as <name>_2.

The game is started directly, not through Steam (it has no Steam API in it). Ubisoft Connect has
to be running and signed in.
"""

import argparse
import ipaddress
import os
import re
import socket
import subprocess
import sys

GAME_FOLDER = "Tom Clancy's Ghost Recon Future Soldier"
# The small launcher: it picks the DX11 or the DX9 build and starts it, and what it starts
# inherits the environment.
LAUNCHER = "Future Soldier.exe"
P2P_PORT = 2346


def steam_libraries():
    """Every Steam library folder on this machine, the main install first."""
    roots = []
    if os.name == "nt":
        import winreg
        for hive, key in ((winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam"),
                          (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam")):
            try:
                with winreg.OpenKey(hive, key) as k:
                    for value in ("SteamPath", "InstallPath"):
                        try:
                            roots.append(os.path.normpath(winreg.QueryValueEx(k, value)[0]))
                        except OSError:
                            pass
            except OSError:
                pass
    found = []
    for root in roots:
        for library in [root] + libraries_in(os.path.join(root, "steamapps",
                                                          "libraryfolders.vdf")):
            if library not in found:
                found.append(library)
    return found


def libraries_in(vdf_path):
    """The "path" entries of a libraryfolders.vdf."""
    try:
        with open(vdf_path, encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return []
    return [os.path.normpath(p.replace("\\\\", "\\"))
            for p in re.findall(r'"path"\s+"([^"]+)"', text)]


def find_game(libraries=None):
    for library in steam_libraries() if libraries is None else libraries:
        folder = os.path.join(library, "steamapps", "common", GAME_FOLDER)
        if os.path.exists(os.path.join(folder, LAUNCHER)):
            return folder
    return None


def port_taken(ip, port=P2P_PORT):
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.bind((ip, port))
        return False
    except OSError:
        return True
    finally:
        probe.close()


def command(folder, exe, ip, extra=()):
    """(argv, cwd, env) for starting the game with its P2P socket on `ip`."""
    env = dict(os.environ, _CLUSTER_NETWORK_NAME_=ip)
    return [os.path.join(folder, exe)] + list(extra), folder, env


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--ip", required=True,
                        help="the address for this copy's UDP 2346: 127.0.0.1, 127.0.0.2, ...")
    parser.add_argument("--game", help="the game folder; by default looked up in Steam")
    parser.add_argument("--exe", default=LAUNCHER,
                        help="what to start in it (default %s; or 'Future Soldier DX11.exe')"
                             % LAUNCHER)
    parser.add_argument("--dry-run", action="store_true", help="print, do not start")
    parser.add_argument("extra", nargs="*", help="passed to the game")
    args = parser.parse_args(argv)

    try:
        ip = str(ipaddress.IPv4Address(args.ip))
    except ValueError:
        parser.error("--ip takes an IPv4 address, such as 127.0.0.2")
    folder = args.game or find_game()
    if not folder or not os.path.exists(os.path.join(folder, args.exe)):
        print("игра не найдена%s - укажи папку: --game \"D:\\...\\%s\""
              % (" в " + folder if folder else " в библиотеках Steam", GAME_FOLDER))
        return 1
    if port_taken(ip):
        print("UDP %s:%d уже занят - там другая копия игры? Возьми другой --ip, например"
              " 127.0.0.%d" % (ip, P2P_PORT, int(ip.rsplit(".", 1)[1]) % 254 + 1))
        return 1

    argv_, cwd, env = command(folder, args.exe, ip, args.extra)
    print("запускаю %s, P2P на %s:%d" % (argv_[0], ip, P2P_PORT))
    if not ip.startswith("127."):
        print("  адрес не 127.x: убедись, что он есть у этой машины и что сервер с него виден")
    print("  на сервере нужен twin_logins = yes, иначе вторая копия выбьет первую")
    if args.dry_run:
        return 0
    flags = 0
    if os.name == "nt":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    subprocess.Popen(argv_, cwd=cwd, env=env, creationflags=flags, close_fds=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
