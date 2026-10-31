"""Checks one player's machine against a GRFS server, the way the game will use it, and says
which step is wrong - in Russian, for the person sitting at that machine.

    python tools/doctor.py                               everything the game needs from here
    python tools/doctor.py p2p --name alice --peer bob   on both machines at the same time

Without --host it finds the server the way the game does: through the hosts file entry for
onlineconfigservice.ubi.com. So the first thing it checks is the thing most often wrong on a
second machine.

The p2p mode is the part nothing else tests. The server only introduces players; in a co-op
match the games send each other UDP directly, port 2346 to port 2346, at the addresses the NAT
echo told them. When that fails the lobby is created and nobody can get in, and from the menu it
looks exactly like a server problem. Both sides run this at once; each signs in, finds the other
through the server the way the game finds a host, and they fire packets at each other until both
directions are proven or the time is up.

Everything printed is also written to doctor-report.txt, to send to whoever runs the server.
Nothing secret is in it: the login uses made-up credentials, as tools/fake_client.py does.
"""

import argparse
import os
import socket
import struct
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from grfs.fakegame import (Peer, Player, RmcFailure, fetch_config, game_address,  # noqa: E402
                           url_address)

CONFIG_HOST = "onlineconfigservice.ubi.com"
HOSTS_FILE = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "drivers",
                          "etc", "hosts")
GAME_P2P_PORT = 2346
FIREWALL_HINT = ('netsh advfirewall firewall add rule name="GRFS P2P" dir=in action=allow'
                 ' protocol=UDP localport=%d')


class Report:
    """Lines on the screen and in the report file; counts what failed."""

    def __init__(self, path=None, colour=True):
        self.path = path
        self.lines = []
        self.failures = 0
        self.warnings = 0
        self.colour = colour and sys.stdout.isatty()
        if self.colour and os.name == "nt":
            os.system("")               # switches the Windows console into ANSI mode

    def _out(self, mark, colour, text):
        self.lines.append(("%s %s" % (mark, text)).rstrip())
        if self.colour and colour:
            print(" \033[%sm%s\033[0m %s" % (colour, mark, text))
        else:
            print(" %s %s" % (mark, text))

    def section(self, title):
        self.lines.append("")
        self.lines.append("== %s" % title)
        print("\n%s" % (("\033[1m%s\033[0m" % title) if self.colour else title))

    def ok(self, text):
        self._out("+", "32", text)

    def bad(self, text):
        self.failures += 1
        self._out("!", "31", text)

    def warn(self, text):
        self.warnings += 1
        self._out("?", "33", text)

    def note(self, text):
        self._out(" ", "90", text)

    def save(self):
        if not self.path:
            return
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                f.write("GRFS doctor, %s, %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"),
                                                   socket.gethostname()))
                f.write("\n".join(self.lines) + "\n")
            print("\nотчёт: %s" % os.path.abspath(self.path))
        except OSError as e:
            print("\nотчёт записать не вышло: %s" % e)


# ----- where the server is -------------------------------------------------------------------
def hosts_entries(path, name=CONFIG_HOST):
    """Addresses the hosts file gives `name`, in file order, or None if it cannot be read."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return None
    found = []
    for line in text.splitlines():
        fields = line.split("#", 1)[0].split()
        if len(fields) >= 2 and name.lower() in (f.lower() for f in fields[1:]):
            found.append(fields[0])
    return found


def check_hosts(report, hosts_file):
    """The server's address as the game will find it, or None."""
    report.section("hosts")
    entries = hosts_entries(hosts_file)
    if entries is None:
        report.warn("не читается %s - проверяю только то, во что имя разрешается" % hosts_file)
    elif not entries:
        report.bad("в %s нет строки для %s. Добавь (от администратора):" % (hosts_file,
                                                                             CONFIG_HOST))
        report.note("<адрес машины с сервером>    %s" % CONFIG_HOST)
        return None
    elif len(entries) > 1:
        report.warn("строк для %s несколько (%s); работает первая" % (CONFIG_HOST,
                                                                       ", ".join(entries)))
    try:
        resolved = socket.gethostbyname(CONFIG_HOST)
    except OSError as e:
        report.bad("%s не разрешается: %s" % (CONFIG_HOST, e))
        return None
    if entries and resolved != entries[0]:
        report.bad("в hosts написано %s, а имя разрешается в %s - строка не действует (опечатка,"
                   " файл сохранён не туда или нужен ipconfig /flushdns)" % (entries[0], resolved))
        return None
    report.ok("%s -> %s" % (CONFIG_HOST, resolved))
    return resolved


def check_config(report, host, port):
    """(auth_port, secure_port, every config value) from the configuration service, or None."""
    report.section("служба конфигурации, TCP %d" % port)
    try:
        values = fetch_config(host, port, every=True)
    except Exception as e:
        report.bad("%s:%d не отвечает: %s" % (host, port, e))
        report.note("сервер не запущен, порт закрыт файрволом на его машине, или в hosts не тот"
                    " адрес")
        return None
    sandbox = (values.get("SandboxUrl") or [""])[0]
    try:
        told, auth_port = url_address(sandbox)
    except Exception:
        report.bad("в ответе нет понятного SandboxUrl: %r" % sandbox)
        return None
    report.ok("SandboxUrl = %s" % sandbox)
    if told in ("0.0.0.0", "", "255.255.255.255"):
        report.bad("сервер говорит игре подключаться к %s - туда подключиться нельзя" % told)
    elif told != host and not told.startswith("127."):
        report.note("сервер называет себя %s, hosts ведёт на %s" % (told, host))
    return auth_port, auth_port + 1, values


def check_login(report, host, auth_port, secure_port, name):
    report.section("вход, UDP %d / %d" % (auth_port, secure_port))
    player = Player(host, auth_port, secure_port, name)
    try:
        player.login()
    except Exception as e:
        report.bad("вход не прошёл: %s" % e)
        report.note("если служба конфигурации ответила, а это нет - закрыт UDP %d/%d на машине"
                    " сервера" % (auth_port, secure_port))
        return False
    finally:
        player.close()
    report.ok("вошёл как %s, pid 0x%X, тикет читается, сервер принял регистрацию"
              % (name, player.pid))
    for connection in (player.auth, player.secure):
        if connection is not None and connection.bad_signatures:
            report.bad("%d пакет(ов) подписаны чужим соединением" % connection.bad_signatures)
    return True


def check_game_address(report, host, secure_port, ip, machine):
    """Where the game's P2P socket will sit, and whether the server can be reached from there.
    False when the game's NAT probe cannot leave that address at all."""
    report.section("адрес игры (UDP %d)" % GAME_P2P_PORT)
    cluster = os.environ.get("_CLUSTER_NETWORK_NAME_")
    if ip is None:
        report.bad("имя %s ни во что не разрешается - игре не к чему привязать порт %d"
                   % (machine, GAME_P2P_PORT))
        if cluster:
            report.note("_CLUSTER_NETWORK_NAME_=%s: туда надо адрес, например 127.0.0.2"
                        % cluster)
        return False
    how = ("из _CLUSTER_NETWORK_NAME_" if cluster
           else "первый адрес имени %s, так его выбирает игра" % machine)
    report.ok("игра сядет на %s:%d (%s)" % (ip, GAME_P2P_PORT, how))
    target = socket.gethostbyname(host)
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.bind((ip, 0))
        probe.connect((target, secure_port))            # picks a route, sends nothing
        return True
    except OSError as e:
        report.bad("с %s до сервера %s пути нет (%s): NAT-проба игры с порта %d не уйдёт"
                   % (ip, target, getattr(e, "winerror", None) or e.errno, GAME_P2P_PORT))
        if target.startswith("127."):
            report.note("сервер на этой машине: в hosts поставь %s вместо 127.0.0.1, или"
                        " запускай игру через launch_game.py --ip 127.0.0.1" % ip)
        else:
            report.note("первым у этой машины стоит адрес другого интерфейса (VPN?). Либо сервер"
                        " должен быть в той же сети, что %s, либо запускай игру через"
                        " launch_game.py --ip <адрес из сети сервера>" % ip)
        return False
    finally:
        probe.close()


def open_p2p_socket(report, port, ip):
    """The socket the game's P2P traffic would use: its address, and its port if free."""
    try:
        return Peer(bind=ip, port=port)
    except OSError:
        if not port:
            raise
        report.warn("порт %d на %s занят - игра запущена? Проверяю с другого порта"
                    % (port, ip))
        return Peer(bind=ip, port=0)


def check_nat_echo(report, host, secure_port, peer):
    """(public ip, public port) of the P2P socket as the server sees it, or None."""
    report.section("NAT echo")
    try:
        url = peer.probe(host, secure_port)
    except ValueError as e:
        report.bad(str(e))
        return None
    if url is None:
        report.bad("на NAT-пробу с UDP %d нет ответа" % peer.address[1])
        return None
    seen = url_address(url)
    local = peer.address[0]
    report.ok("сервер видит этот компьютер как %s:%d" % seen)
    if seen[0] == local or seen[0].startswith("127."):
        report.note("между тобой и сервером нет NAT - одна сеть")
    else:
        report.note("ты за NAT: внутри %s, снаружи %s" % (local, seen[0]))
        if seen[1] != peer.address[1]:
            report.note("NAT меняет порт (%d -> %d)" % (peer.address[1], seen[1]))
    return seen


# ----- NAT type, as the game's Storm detector works it out ------------------------------------
class NatTypeProbe:
    """The client half of the punch_DetectUrls exchange, step for step as the game runs it
    (notes: the FSM at 9333B0). 13-byte requests '00 0D | cmd | seq | 00 | 7 bytes', 19-byte
    replies '00 13 | type | 9 bytes | mapped IP byte-reversed | mapped port'. The game reports
    what this reports, because the server alone decides the answers."""

    TIMEOUT = 2.5               # the game's packet timeout
    RETRIES = 3                 # cmd 3 is resent this many times before giving up on it

    def __init__(self, s1, s2, timeout=None, local_ip=None):
        self.s1, self.s2 = s1, s2
        self.timeout = timeout or self.TIMEOUT
        self.local_ip = local_ip or _first_local_ip()
        self.seq = 0
        self.steps = []

    def ask(self, sock, cmd, target):
        self.seq = (self.seq + 1) & 0xFF
        sock.sendto(struct.pack(">HHB", 13, cmd, self.seq) + bytes(8), target)
        deadline = time.monotonic() + self.timeout
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                self.steps.append("cmd %d -> %s:%d: нет ответа" % (cmd, *target))
                return None
            sock.settimeout(left)
            try:
                data, _ = sock.recvfrom(64)
            except socket.timeout:
                continue
            except ConnectionResetError:
                continue
            reply = parse_reply(data)
            if reply is None:
                continue
            self.steps.append("cmd %d -> %s:%d: тип %d, %s:%d" % (cmd, *target, *reply))
            return reply

    def run(self):
        """('Open' | 'Moderate' | 'Strict' | 'Unknown', why)."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.bind(("0.0.0.0", 0))
            return self._run(sock)
        finally:
            sock.close()

    def _run(self, sock):
        first = self.ask(sock, 1, self.s1)                                   # Filter1
        if first is None or first[0] != 1:
            return "Unknown", "первый сервер детекта (%s:%d) не отвечает" % self.s1
        mapped = first[1:]
        if mapped[0] == self.local_ip:
            return "Open", "NAT нет: снаружи тот же адрес, что внутри"
        filtering = 3
        reply = None
        for _ in range(self.RETRIES + 1):                                    # Filter2
            reply = self.ask(sock, 3, self.s1)
            if reply is not None:
                break
        if reply is None:
            filtering = 2
            reply = self.ask(sock, 1, self.s2)
            if reply is None:
                return "Unknown", "второй сервер детекта (%s:%d) не отвечает" % self.s2
        if reply[0] == 3:                                                    # Filter3
            filtering = 1
            reply = self.ask(sock, 2, self.s1)
            if reply is None or reply[0] != 2:
                return "Unknown", "нет ответа с другого порта"
        elif reply[0] != 1:
            return "Unknown", "неожиданный ответ типа %d" % reply[0]
        again = self.ask(sock, 1, (self.s1[0], self.s1[1] + 1))              # Map1
        if again is None:
            return "Unknown", "порт %d сервера детекта не отвечает" % (self.s1[1] + 1)
        if again[2] == mapped[1]:
            return (("Open", "NAT держит порт и пропускает чужие ответы") if filtering == 1
                    else ("Moderate", "NAT держит порт, но фильтрует входящие"))
        self.ask(sock, 1, self.s2)                                           # Map2
        return "Strict", "NAT меняет порт под каждого адресата (симметричный)"


def parse_reply(data):
    """(type, mapped ip, mapped port) out of a detect reply, or None if the game would drop it."""
    if len(data) != 19:
        return None
    length, kind = struct.unpack_from(">HH", data)
    if length > len(data) or kind not in (1, 2, 3):
        return None
    ip = socket.inet_ntoa(data[13:17][::-1])
    return kind, ip, struct.unpack_from(">H", data, 17)[0]


def _first_local_ip():
    try:
        return socket.gethostbyname(socket.gethostname())
    except OSError:
        return "127.0.0.1"


def _host_port(value):
    host, _, port = value.rpartition(":")
    return socket.gethostbyname(host), int(port)


def check_nat_type(report, values, timeout=None):
    report.section("тип NAT (punch_DetectUrls)")
    servers = values.get("punch_DetectUrls") or []
    if len(servers) < 2:
        report.warn("сервер не раздаёт punch_DetectUrls двумя значениями - игра покажет тип NAT"
                    " Unknown. Сам по себе Unknown вход не запрещает, но меню может ругаться")
        return "Unknown"
    try:
        s1, s2 = _host_port(servers[0]), _host_port(servers[1])
    except (ValueError, OSError):
        report.bad("punch_DetectUrls не в виде ip:port: %s - детект у игры не запустится"
                   % servers)
        return "Unknown"
    probe = NatTypeProbe(s1, s2, timeout=timeout)
    kind, why = probe.run()
    for step in probe.steps:
        report.note(step)
    if kind == "Open":
        report.ok("Open - %s" % why)
    elif kind == "Moderate":
        report.warn("Moderate - %s" % why)
    elif kind == "Strict":
        report.bad("Strict - %s. С таким NAT игра не даст стать хостом" % why)
    else:
        report.warn("Unknown - %s" % why)
    return kind


# ----- two players, straight at each other ---------------------------------------------------
MAGIC = b"GRFS-P2P/1 "


def find_peer(player, peer_name, wait, report):
    """The other player's registered udp:/ addresses, asked for the way the game asks."""
    deadline = time.monotonic() + wait
    said = False
    while time.monotonic() < deadline:
        pids = dict(player.call("UbiAccountManagementProtocol", "LookupPrincipalIds",
                                {"ubiAccountIds": [peer_name]})["pids"])
        if pids:
            urls = player.call("SecureConnectionProtocol", "RequestURLs",
                               {"cidTarget": 0, "pidTarget": next(iter(pids.values()))})
            found = list(dict.fromkeys(url_address(u) for u in urls["plstURLs"]
                                       if u.lower().startswith("udp:"))) if urls["retval"] else []
            if found:
                return found
        if not said:
            report.note("жду, пока %s запустит то же самое (до %d с)..." % (peer_name, wait))
            said = True
        player.secure.ping()
        time.sleep(1.0)
    return []


def exchange(sock, me, targets, seconds):
    """Fire HELLOs at every address of the peer and answer theirs. Returns (heard_from,
    acked_by): where their HELLO came from, and which of our targets answered."""
    heard_from = acked_by = None
    deadline = time.monotonic() + seconds
    done_at = None
    seq = 0
    sock.settimeout(0.05)
    while time.monotonic() < deadline:
        if heard_from and acked_by:
            # Keep answering a little longer: the other side may still be waiting for our ACK.
            done_at = done_at or time.monotonic()
            if time.monotonic() - done_at > 2.0:
                break
        seq += 1
        for target in targets:
            try:
                sock.sendto(MAGIC + b"HELLO %s %d %s:%d" % (me.encode(), seq,
                                                            target[0].encode(), target[1]),
                            target)
            except OSError:
                pass
        until = time.monotonic() + 0.2
        while time.monotonic() < until:
            try:
                data, source = sock.recvfrom(512)
            except (socket.timeout, ConnectionResetError):
                continue
            except OSError:
                break
            if not data.startswith(MAGIC):
                continue
            words = data[len(MAGIC):].decode("latin-1").split()
            if words[0] == "HELLO":
                heard_from = heard_from or source
                sock.sendto(MAGIC + b"ACK %s %s" % (me.encode(), words[3].encode()), source)
            elif words[0] == "ACK" and len(words) >= 3:
                host, _, port = words[2].rpartition(":")
                acked_by = acked_by or (host, int(port))
    return heard_from, acked_by


def check_p2p(report, host, auth_port, secure_port, name, peer_name, p2p_port, wait, seconds,
              ip):
    report.section("P2P: %s <-> %s" % (name, peer_name))
    peer_socket = open_p2p_socket(report, p2p_port, ip)
    with peer_socket:
        seen = check_nat_echo(report, host, secure_port, peer_socket)
        if seen is None:
            return False
        urls = ["udp:/address=%s;port=%d" % seen]
        inside = "udp:/address=%s;port=%d" % peer_socket.address
        if inside not in urls:
            urls.append(inside)             # what a player on the same network connects to
        player = Player(host, auth_port, secure_port, name, urls=urls)
        try:
            player.login()
            report.ok("вошёл как %s, зарегистрировал %s" % (name, ", ".join(urls)))
            targets = find_peer(player, peer_name, wait, report)
            if not targets:
                report.bad("%s так и не появился на сервере. Он запустил doctor p2p с --name %s?"
                           % (peer_name, peer_name))
                return False
            report.ok("сервер дал адреса %s: %s" % (peer_name, ", ".join(
                "%s:%d" % t for t in targets)))
            heard_from, acked_by = exchange(peer_socket.sock, name, targets, seconds)
        except (RmcFailure, TimeoutError, OSError) as e:
            report.bad("сервер перестал отвечать: %s" % e)
            return False
        finally:
            player.close()
    return verdict(report, name, peer_name, heard_from, acked_by, peer_socket.address[1])


def verdict(report, name, peer_name, heard_from, acked_by, port):
    if heard_from and acked_by:
        report.ok("работает в обе стороны: до %s дошло на %s:%d, от него пришло с %s:%d"
                  % ((peer_name,) + tuple(acked_by) + tuple(heard_from)))
        return True
    if heard_from:
        report.bad("от %s к тебе доходит (с %s:%d), от тебя к нему - нет. Входящий UDP %d"
                   " закрыт у %s: его файрвол или роутер" % (peer_name, heard_from[0],
                                                             heard_from[1], port, peer_name))
        report.note("у %s, в консоли администратора: %s" % (peer_name, FIREWALL_HINT % port))
        return False
    if acked_by:
        report.bad("%s тебя слышит, но шлёт не туда - сервер раздаёт ему не тот твой адрес"
                   % peer_name)
        return False
    report.bad("пакеты не прошли ни в одну сторону. Входящий UDP %d закрыт у обоих или NAT"
               " у обоих не пропускает встречные пакеты" % port)
    report.note("на обеих машинах, в консоли администратора: %s" % (FIREWALL_HINT % port))
    report.note("если не помогло - проброс UDP %d на роутере хотя бы у одного" % port)
    return False


# ----- the whole run -------------------------------------------------------------------------
def locate(report, args):
    """(host, auth_port, secure_port, config values) or None."""
    host = args.host or check_hosts(report, args.hosts_file)
    if not host:
        return None
    found = check_config(report, host, args.config_port)
    if found is None:
        return None
    auth_port, secure_port, values = found
    return host, auth_port, secure_port, values


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("mode", nargs="?", choices=("check", "p2p"), default="check")
    parser.add_argument("--host", help="the server's address; by default the hosts file's")
    parser.add_argument("--config-port", type=int, default=80)
    parser.add_argument("--hosts-file", default=HOSTS_FILE)
    parser.add_argument("--name", help="account to sign in as (p2p: your name, as the other"
                                       " side types it)")
    parser.add_argument("--peer", help="p2p: the other player's --name")
    parser.add_argument("--p2p-port", type=int, default=GAME_P2P_PORT)
    parser.add_argument("--game-ip", help="the address the game's P2P socket binds to; by"
                                          " default the one the game itself would pick")
    parser.add_argument("--wait", type=int, default=120,
                        help="p2p: seconds to wait for the other side to show up")
    parser.add_argument("--time", type=int, default=20, help="p2p: seconds of packet exchange")
    parser.add_argument("--detect-timeout", type=float, help=argparse.SUPPRESS)
    parser.add_argument("--report", default="doctor-report.txt")
    parser.add_argument("--no-colour", action="store_true")
    args = parser.parse_args(argv)

    if args.mode == "p2p":
        if not args.name and sys.stdin.isatty():
            args.name = input("твоё имя для проверки: ").strip()
        if not args.peer and sys.stdin.isatty():
            args.peer = input("имя второго игрока: ").strip()
        if not args.name or not args.peer or args.name == args.peer:
            parser.error("p2p needs --name and --peer, and they must differ")

    report = Report(args.report, colour=not args.no_colour)
    print("GRFS doctor - %s" % socket.gethostname())
    machine, ip = game_address()
    ip = args.game_ip or ip
    try:
        located = locate(report, args)
        if located is not None:
            host, auth_port, secure_port, values = located
            reachable = check_game_address(report, host, secure_port, ip, machine)
            if args.mode == "check":
                if check_login(report, host, auth_port, secure_port,
                               args.name or "doctor-%s" % socket.gethostname().lower()) \
                        and reachable:
                    with open_p2p_socket(report, args.p2p_port, ip) as peer:
                        check_nat_echo(report, host, secure_port, peer)
                check_nat_type(report, values, args.detect_timeout)
            elif reachable:
                check_p2p(report, host, auth_port, secure_port, args.name, args.peer,
                          args.p2p_port, args.wait, args.time, ip)
    except KeyboardInterrupt:
        report.bad("прервано")

    report.section("итог")
    if report.failures:
        report.bad("не в порядке: %d" % report.failures)
    elif report.warnings:
        report.ok("в порядке, с замечаниями: %d" % report.warnings)
    else:
        report.ok("всё в порядке")
    report.save()
    return 1 if report.failures else 0


if __name__ == "__main__":
    sys.exit(main())
