"""The real game against this server, run and watched without anybody at the keyboard: a server
of its own, copies of the game started on addresses of their own, each copy followed through
the server's log, and its window photographed and given keys and clicks.

    python tools/rig.py check                 what is missing on this machine, and why
    python tools/rig.py up [--copies 2]       the server, then the copies (127.0.0.1, .2, ...)
    python tools/rig.py status                each copy: process, window, how far it got
    python tools/rig.py wait 1 menu           until copy 1 reaches a step (see STEPS)
    python tools/rig.py shot [1]              a PNG of copy 1's window, or of the screen
    python tools/rig.py keys 1 down down enter
    python tools/rig.py type 1 "text"
    python tools/rig.py click 1 320 240 [--right] [--double]
    python tools/rig.py tile                  windows side by side
    python tools/rig.py log [--tail 40] [--grep RE] [--problems]
    python tools/rig.py down                  copies and server stopped

Every command is a separate run; what they share is in logs/rig/state.json. The server keeps
its register in logs/rig/data, never in data/: a test run must not leave accounts behind.

How far a copy got is read from the server's log, the one witness that does not need the
screen: the configuration fetched, signed in, registered, the NAT echo, the main menu (its
first act there is asking for the map list), a session hosted or joined. A copy's lines are
found by its name; its name by the address it signed in from, or, when both copies sign in
from the same address, by the order they did.
"""

import argparse
import json
import os
import re
import socket
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

import launch_game  # noqa: E402

RIG_DIR = os.path.join(ROOT, "logs", "rig")
CONFIG_HOST = "onlineconfigservice.ubi.com"
HOSTS_FILE = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "drivers",
                          "etc", "hosts")
GAME_EXE = "Future Soldier DX11.exe"
UBISOFT_CONNECT = ("upc.exe", "UbisoftConnect.exe", "UbisoftGameLauncher.exe")
UBISOFT_CONNECT_DIRS = [os.path.join(os.environ.get(v, ""), "Ubisoft", "Ubisoft Game Launcher")
                        for v in ("ProgramFiles(x86)", "ProgramFiles")]
PORTS = {"onlineconfig_port": 80, "auth_port": 21030, "secure_port": 21031, "punch_port": 21032,
         "panel_port": 8080}

# step -> pattern in the server's log. {name} is the copy's player name, {ip} its address.
STEPS = [
    ("signed-in", r"{ip} signed in as (?P<name>\S+) \(pid"),
    ("registered", r"{name} registered \d+ station URL"),
    ("nat-echo", r"told \('{ip}', \d+\) its public address"),
    ("menu", r"{name} may host \d+ world"),
    ("hosting", r"{name} hosts session (?P<session>\d+)"),
    ("joining", r"{name} may join session (?P<session>\d+)"),
    ("in-session", r"session (?P<session>\d+): \S+ added .*{name}"),
    ("invited", r"{name} invited (?P<other>\S+) to session"),
    ("accepted", r"{name} accepted (?P<other>\S+)'s invitation"),
]
STEP_NAMES = [name for name, _ in STEPS]
PROBLEM = re.compile(r" (WARN|ERROR|CRIT)")


# ----- state ----------------------------------------------------------------------------------
class Rig:
    def __init__(self, rig_dir=RIG_DIR):
        self.dir = rig_dir
        self.state_path = os.path.join(rig_dir, "state.json")
        self.log_path = os.path.join(rig_dir, "server.log")
        self.state = {"server": None, "copies": []}
        if os.path.exists(self.state_path):
            with open(self.state_path, encoding="utf-8") as f:
                self.state = json.load(f)

    def save(self):
        os.makedirs(self.dir, exist_ok=True)
        with open(self.state_path, "w", encoding="utf-8") as f:
            json.dump(self.state, f, indent=1)

    def copy(self, n):
        for c in self.state["copies"]:
            if c["n"] == n:
                return c
        raise SystemExit("no copy %d - `rig.py status` lists them" % n)

    # ----- the server -------------------------------------------------------------------------
    def server_up(self, ports=None, settings=None):
        if self.state["server"] and _alive(self.state["server"]["pid"]):
            return self.state["server"]
        ports = dict(PORTS, **(ports or {}))
        os.makedirs(self.dir, exist_ok=True)
        ini = os.path.join(self.dir, "grfs.ini")
        values = dict({"bind": "0.0.0.0", "log_level": "debug", "log_file": self.log_path,
                       "twin_logins": "yes", "data_dir": os.path.join(self.dir, "data")},
                      **{k: str(v) for k, v in ports.items()}, **(settings or {}))
        with open(ini, "w", encoding="utf-8") as f:
            f.write("[server]\n" + "".join("%s = %s\n" % kv for kv in values.items()))
        mark = os.path.getsize(self.log_path) if os.path.exists(self.log_path) else 0
        console = open(os.path.join(self.dir, "server-console.txt"), "ab")
        flags = (subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
                 if os.name == "nt" else 0)
        with console:
            process = subprocess.Popen([sys.executable, "-m", "grfs", "--config", ini], cwd=ROOT,
                                       stdout=console, stderr=subprocess.STDOUT,
                                       creationflags=flags)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            text = self.log_text(mark)
            if " ready: " in text:
                break
            if process.poll() is not None:
                raise SystemExit("the server stopped at once:\n" + (
                    text or open(console.name, encoding="utf-8", errors="replace").read()[-2000:]))
            time.sleep(0.1)
        else:
            process.kill()
            raise SystemExit("the server did not say ready in 15 s; see %s" % self.log_path)
        self.state["server"] = {"pid": process.pid, "ports": ports, "log_from": mark,
                                "started": time.time()}
        self.state["copies"] = []
        self.save()
        return self.state["server"]

    def log_text(self, start=None):
        if not os.path.exists(self.log_path):
            return ""
        if start is None:
            start = (self.state.get("server") or {}).get("log_from", 0)
        with open(self.log_path, "rb") as f:
            f.seek(start)
            return f.read().decode("utf-8", "replace")

    # ----- the copies -------------------------------------------------------------------------
    def launch(self, n, game=None, exe=GAME_EXE, command=None, extra=()):
        """Start copy n with its P2P socket on 127.0.0.n. `command` replaces the game - for the
        tests, which start a stand-in window."""
        ip = "127.0.0.%d" % n
        if command:
            argv, cwd, env = list(command), ROOT, dict(os.environ, _CLUSTER_NETWORK_NAME_=ip)
        else:
            folder = game or launch_game.find_game()
            if not folder:
                raise SystemExit("the game is not in any Steam library; pass --game")
            if launch_game.port_taken(ip):
                raise SystemExit("%s:%d is taken - a copy is already running there" % (
                    ip, launch_game.P2P_PORT))
            argv, cwd, env = launch_game.command(folder, exe, ip, extra)
        process = subprocess.Popen(argv, cwd=cwd, env=env, close_fds=True)
        entry = {"n": n, "ip": ip, "pid": process.pid, "argv": argv, "started": time.time()}
        self.state["copies"] = [c for c in self.state["copies"] if c["n"] != n] + [entry]
        self.state["copies"].sort(key=lambda c: c["n"])
        self.save()
        return entry

    def game_pid(self, copy):
        """The process drawing the copy's window: what was started, or what that started (the
        small Future Soldier.exe starts the DX11 or the DX9 build and leaves)."""
        from grfs import desktop
        pid = copy["pid"]
        if desktop.alive(pid):
            return pid
        for child, _ in desktop.children(pid):
            return child
        return None

    def window(self, n):
        from grfs import desktop
        desktop.dpi_aware()
        copy = self.copy(n)
        pid = self.game_pid(copy)
        window = desktop.main_window(pid) if pid else None
        if window is None:
            raise SystemExit("copy %d has no window (process %s)" % (n, pid or "gone"))
        return window

    # ----- what the log says ------------------------------------------------------------------
    def names(self, text=None):
        """copy n -> the name it signed in as, or None yet."""
        text = self.log_text() if text is None else text
        return assign_names(text, self.state["copies"])

    def progress(self, n, text=None):
        text = self.log_text() if text is None else text
        copy = self.copy(n)
        return steps_reached(text, copy["ip"], self.names(text).get(n))

    def stop_all(self):
        stopped = []
        for copy in self.state["copies"]:
            for pid in {copy["pid"], self.game_pid(copy) or copy["pid"]}:
                if _kill(pid):
                    stopped.append("copy %d (%d)" % (copy["n"], pid))
        server = self.state.get("server")
        if server and _kill(server["pid"]):
            stopped.append("server (%d)" % server["pid"])
        self.state = {"server": None, "copies": []}
        self.save()
        return stopped


def assign_names(text, copies):
    """Which name each copy signed in under: the sign-in from its own address, else the n-th
    distinct player name in the order the copies were started."""
    signed = [(m.group(1), m.group(2)) for m in re.finditer(
        r" (\d+\.\d+\.\d+\.\d+) signed in as (\S+) \(pid", text) if m.group(2) != "Tracking"]
    by_ip = {}
    for ip, name in signed:
        by_ip.setdefault(ip, [])
        if name not in by_ip[ip]:
            by_ip[ip].append(name)
    order = []
    for _, name in signed:
        if name not in order:
            order.append(name)
    out = {}
    taken = set()
    for copy in copies:
        mine = [name for name in by_ip.get(copy["ip"], []) if len(by_ip[copy["ip"]]) == 1]
        if mine:
            out[copy["n"]] = mine[0]
            taken.add(mine[0])
    rest = [name for name in order if name not in taken]
    for copy in copies:
        if copy["n"] not in out and rest:
            out[copy["n"]] = rest.pop(0)
    for copy in copies:
        out.setdefault(copy["n"], None)
    return out


def steps_reached(text, ip, name):
    """step -> the details of the first log line that shows it (a dict, maybe empty)."""
    reached = {}
    for step, pattern in STEPS:
        if "{name}" in pattern and not name:
            continue
        if step == "signed-in" and name:
            found = re.search(r" \S+ signed in as %s \(pid" % re.escape(name), text)
        else:
            found = re.search(pattern.format(ip=re.escape(ip), name=re.escape(name or "")), text)
        if found:
            reached[step] = {k: v for k, v in found.groupdict().items() if v}
    return reached


# ----- the machine ----------------------------------------------------------------------------
def preflight(hosts_file=HOSTS_FILE, ports=None, game=None, processes=None):
    """[(ok, what, how to fix)] - everything the real game needs here before `up`."""
    out = []
    entries = _hosts_entries(hosts_file)
    if entries is None:
        out.append((False, "hosts не читается: %s" % hosts_file, ""))
    elif not entries:
        out.append((False, "в hosts нет строки для %s" % CONFIG_HOST,
                    "один раз, от администратора: powershell -ExecutionPolicy Bypass -File"
                    " tools\\setup_once.ps1"))
    elif entries[0] != "127.0.0.1":
        out.append((False, "hosts ведёт %s на %s, а копии игры здесь ходят на 127.0.0.1"
                    % (CONFIG_HOST, entries[0]), "поменяй адрес в той строке на 127.0.0.1"))
    else:
        out.append((True, "hosts: %s -> 127.0.0.1" % CONFIG_HOST, ""))

    folder = game or launch_game.find_game()
    out.append((bool(folder), "игра: %s" % (folder or "не найдена в библиотеках Steam"),
                "" if folder else "укажи --game"))

    running = {name.lower() for name in (processes if processes is not None
                                         else _process_names())}
    installed = any(os.path.isdir(d) for d in UBISOFT_CONNECT_DIRS)
    if any(name.lower() in running for name in UBISOFT_CONNECT):
        out.append((True, "Ubisoft Connect запущен", ""))
    elif installed:
        out.append((False, "Ubisoft Connect установлен, но не запущен",
                    "запусти его и войди в аккаунт, на котором есть игра"))
    else:
        out.append((False, "Ubisoft Connect не установлен - без него игра не получит логин",
                    "setup_once.ps1 запустит установщик из папки игры; потом войти в аккаунт"))

    busy = [("%s %d" % kind_port) for kind_port in _busy_ports(ports or PORTS)]
    out.append((not busy, "порты сервера свободны" if not busy else
                "заняты: %s" % ", ".join(busy),
                "" if not busy else "закрой другой сервер (python -m grfs) или rig.py down"))
    return out


def _hosts_entries(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except OSError:
        return None
    found = []
    for line in lines:
        fields = line.split("#", 1)[0].split()
        if len(fields) >= 2 and CONFIG_HOST in (n.lower() for n in fields[1:]):
            found.append(fields[0])
    return found


def _busy_ports(ports):
    busy = []
    for key, port in ports.items():
        if not port:
            continue
        kind = socket.SOCK_STREAM if key in ("onlineconfig_port", "panel_port") \
            else socket.SOCK_DGRAM
        for p in ([port, port + 1] if key == "punch_port" else [port]):
            s = socket.socket(socket.AF_INET, kind)
            try:
                s.bind(("0.0.0.0", p))
            except OSError:
                busy.append(("TCP" if kind == socket.SOCK_STREAM else "UDP", p))
            finally:
                s.close()
    return busy


def _process_names():
    try:
        from grfs import desktop
        return [name for _, _, name in desktop.processes()]
    except Exception:
        return []


def _alive(pid):
    from grfs import desktop
    return desktop.alive(pid)


def _kill(pid):
    if not pid or not _alive(pid):
        return False
    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
    return True


# ----- the command line -----------------------------------------------------------------------
def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dir", default=RIG_DIR, help=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest="action", required=True)
    p = sub.add_parser("check")
    p.add_argument("--hosts-file", default=HOSTS_FILE)
    p = sub.add_parser("up")
    p.add_argument("--copies", type=int, default=2)
    p.add_argument("--game")
    p.add_argument("--exe", default=GAME_EXE)
    p.add_argument("--skip-check", action="store_true")
    p.add_argument("--port", action="append", default=[], metavar="KEY=PORT",
                   help=argparse.SUPPRESS)
    p.add_argument("--command", help=argparse.SUPPRESS)       # a stand-in instead of the game
    p = sub.add_parser("launch")
    p.add_argument("n", type=int)
    p.add_argument("--game")
    p.add_argument("--exe", default=GAME_EXE)
    p.add_argument("--command", help=argparse.SUPPRESS)
    sub.add_parser("status")
    p = sub.add_parser("wait")
    p.add_argument("n", type=int)
    p.add_argument("step", choices=STEP_NAMES)
    p.add_argument("--timeout", type=float, default=180)
    p = sub.add_parser("shot")
    p.add_argument("n", type=int, nargs="?")
    p.add_argument("--out")
    p = sub.add_parser("keys")
    p.add_argument("n", type=int)
    p.add_argument("names", nargs="+")
    p.add_argument("--gap", type=float, default=0.35)
    p = sub.add_parser("type")
    p.add_argument("n", type=int)
    p.add_argument("text")
    p = sub.add_parser("click")
    p.add_argument("n", type=int)
    p.add_argument("x", type=int)
    p.add_argument("y", type=int)
    p.add_argument("--right", action="store_true")
    p.add_argument("--double", action="store_true")
    sub.add_parser("tile")
    p = sub.add_parser("log")
    p.add_argument("--tail", type=int, default=40)
    p.add_argument("--grep")
    p.add_argument("--problems", action="store_true")
    sub.add_parser("down")
    args = parser.parse_args(argv)
    rig = Rig(args.dir)
    return COMMANDS[args.action](rig, args)


def cmd_check(rig, args):
    failed = 0
    for ok, what, fix in preflight(args.hosts_file):
        print(" %s %s" % ("+" if ok else "!", what))
        if fix:
            print("     %s" % fix)
        failed += not ok
    return 1 if failed else 0


def cmd_up(rig, args):
    ports = dict(PORTS, **{k: int(v) for k, v in (p.split("=", 1) for p in args.port)})
    if not args.skip_check and not args.command:
        problems = [(what, fix) for ok, what, fix in preflight(ports=ports, game=args.game)
                    if not ok and not what.startswith("заняты")]
        if problems:
            for what, fix in problems:
                print(" ! %s\n     %s" % (what, fix))
            return 1
    server = rig.server_up(ports)
    print("server: pid %d, log %s" % (server["pid"], rig.log_path))
    command = args.command.split("|") if args.command else None
    for n in range(1, args.copies + 1):
        copy = rig.launch(n, args.game, args.exe, command)
        print("copy %d: pid %d, P2P on %s:2346" % (n, copy["pid"], copy["ip"]))
        time.sleep(2)
    return 0


def cmd_launch(rig, args):
    copy = rig.launch(args.n, args.game, args.exe,
                      args.command.split("|") if args.command else None)
    print("copy %d: pid %d, P2P on %s:2346" % (copy["n"], copy["pid"], copy["ip"]))
    return 0


def cmd_status(rig, args):
    server = rig.state.get("server")
    if not server:
        print("nothing is up")
        return 1
    print("server: pid %d %s, log %s" % (server["pid"], "running" if _alive(server["pid"])
                                         else "GONE", rig.log_path))
    text = rig.log_text()
    names = rig.names(text)
    for copy in rig.state["copies"]:
        pid = rig.game_pid(copy)
        try:
            window = rig.window(copy["n"])
            seen = "window %r %dx%d" % (window.title, window.rect[2], window.rect[3])
        except SystemExit:
            seen = "no window"
        reached = steps_reached(text, copy["ip"], names[copy["n"]])
        print("copy %d (%s): pid %s, %s, name %s" % (copy["n"], copy["ip"], pid or "GONE",
                                                     seen, names[copy["n"]] or "-"))
        print("   " + " ".join(("%s%s" % (s, "=" + ",".join(reached[s].values())
                                          if reached.get(s) else "")) if s in reached
                               else "." for s in STEP_NAMES))
    problems = [line for line in text.splitlines() if PROBLEM.search(line)]
    print("problems in the log: %d%s" % (len(problems), " (rig.py log --problems)"
                                         if problems else ""))
    return 0


def cmd_wait(rig, args):
    deadline = time.monotonic() + args.timeout
    while time.monotonic() < deadline:
        reached = rig.progress(args.n)
        if args.step in reached:
            print("copy %d: %s %s" % (args.n, args.step, reached[args.step] or ""))
            return 0
        copy = rig.copy(args.n)
        if not rig.game_pid(copy) and time.time() - copy["started"] > 20:
            print("copy %d is gone; it got as far as: %s" % (args.n, ", ".join(reached) or
                                                              "nothing"))
            return 1
        time.sleep(0.5)
    print("copy %d did not reach %s in %d s; it got as far as: %s" % (
        args.n, args.step, args.timeout, ", ".join(rig.progress(args.n)) or "nothing"))
    return 1


def _shot_path(rig, what):
    return os.path.join(rig.dir, "shots", "%s-%s.png" % (time.strftime("%H%M%S"), what))


def cmd_shot(rig, args):
    from grfs import desktop
    desktop.dpi_aware()
    if args.n is None:
        path = desktop.screenshot(args.out or _shot_path(rig, "screen"))
    else:
        window = rig.window(args.n)
        path = window.screenshot(args.out or _shot_path(rig, "copy%d" % args.n))
        print("%dx%d" % window.rect[2:])
    print(path)
    return 0


def _drive(rig, n, action):
    from grfs import desktop
    try:
        action(rig.window(n))
    except desktop.DesktopError as e:
        print("not sent: %s" % e)
        return 1
    return 0


def cmd_keys(rig, args):
    return _drive(rig, args.n, lambda w: w.keys(args.names, gap=args.gap))


def cmd_type(rig, args):
    return _drive(rig, args.n, lambda w: w.type(args.text))


def cmd_click(rig, args):
    return _drive(rig, args.n, lambda w: w.click(args.x, args.y,
                                                 "right" if args.right else "left", args.double))


def cmd_tile(rig, args):
    from grfs import desktop
    desktop.dpi_aware()
    copies = rig.state["copies"]
    if not copies:
        return 1
    screen_w = desktop.user32.GetSystemMetrics(0)
    screen_h = desktop.user32.GetSystemMetrics(1)
    width = screen_w // len(copies)
    for i, copy in enumerate(copies):
        rig.window(copy["n"]).move(i * width, 0, width, min(screen_h, width * 3 // 4 + 40))
    return 0


def cmd_log(rig, args):
    lines = rig.log_text().splitlines()
    if args.problems:
        lines = [line for line in lines if PROBLEM.search(line)]
    if args.grep:
        lines = [line for line in lines if re.search(args.grep, line)]
    for line in lines[-args.tail:]:
        print(line)
    return 0


def cmd_down(rig, args):
    stopped = rig.stop_all()
    print("stopped: %s" % (", ".join(stopped) or "nothing was running"))
    return 0


COMMANDS = {"check": cmd_check, "up": cmd_up, "launch": cmd_launch, "status": cmd_status,
            "wait": cmd_wait, "shot": cmd_shot, "keys": cmd_keys, "type": cmd_type,
            "click": cmd_click, "tile": cmd_tile, "log": cmd_log, "down": cmd_down}


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    sys.exit(main())
