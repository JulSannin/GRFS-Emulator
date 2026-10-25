"""The backend: everything the servers share.

One of these holds the configuration, the protocol schema, the clients, the account register and
the services. The UDP endpoints (net.PrudpServer) are thin by comparison - they turn datagrams
into calls on this.
"""

import os
import socket
import time

from . import log as logs
from . import protocols
from .accounts import Accounts
from .config import Config
from .friends import Friends
from .kerberos import SERVER_PID
from .nat import NatEcho
from .prudp import packet as P
from .prudp.connection import ClientTable
from .rmc.ddl import Schema
from .rmc.router import Router

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCHEMA_PATH = os.path.join(ROOT, "schema", "grfs-ddl.json")
DATA_DIR = os.path.join(ROOT, "data")

# How long a player may go quiet before they count as away. The game sends something about once
# a second - a ping if nothing else - and every packet counts, so this is generous. It used to be
# 45 seconds against RMC calls only, and a player sitting on a menu makes none: they went
# offline in everyone's list, and their open session was pruned out from under them.
PRESENCE_WINDOW = 90.0

# How long a record is kept after that. Longer than the presence window on purpose: the record
# holds the addresses other players reach this one at, and a player who steps away for a minute
# should not have them thrown out from under them.
FORGET_AFTER = 600.0


class Backend:
    def __init__(self, config: Config = None, schema_path: str = None, data_dir: str = None):
        self.config = config or Config()
        self.log = logs.get("backend")
        self.started = time.time()
        self.schema = Schema().load_ddl_json(schema_path or SCHEMA_PATH)
        self.protocol_names = dict(protocols.NAMES)
        self.data_dir = data_dir if data_dir is not None else DATA_DIR
        self.accounts = Accounts(os.path.join(self.data_dir, "accounts.json")
                                 if self.data_dir else None)
        self.friends = Friends(self.accounts,
                               os.path.join(self.data_dir, "friends.json")
                               if self.data_dir else None,
                               everyone=self.config.bool("auto_friends"))
        if self.friends.everyone:
            self.log.info("auto_friends is on: everyone with an account here is everyone else's"
                          " friend, and adding or removing is ignored")
        self.clients = ClientTable()
        self.router = Router(self)
        self.nat = NatEcho(self)
        self.server_pid = SERVER_PID
        self.auth_port = self.config.int("auth_port", 21030)
        self.secure_port = self.config.int("secure_port", 21031)
        self.game_port = self.config.int("game_port", 2347)
        self._advertised = {}
        self.servers = {}
        self.install_services()

    def install_services(self) -> None:
        from .services import all_services
        for factory in all_services():
            service = self.router.add(factory(self))
            self.log.debug("%s (0x%02X): %s", service.protocol_name, service.protocol_id,
                           ", ".join(name for _, name in service.handled()) or "nothing yet")

    # ----- odds and ends --------------------------------------------------------------------
    def now(self) -> float:
        return time.time()

    def server_vport(self, client_vport: int) -> int:
        """Our end of a connection: port 1 on the same stream the client is using."""
        return (P.stream_of(client_vport) << 4) | 1

    # ----- players --------------------------------------------------------------------------
    def is_online(self, pid: int) -> bool:
        """Whether this player is here now, worked out from when they were last heard from.

        Presence needs no protocol: the game sends packets constantly and every one of them is
        stamped. Silence is the only honest signal there is.
        """
        if not pid:
            return False
        client = self.clients.by_pid(pid)
        return client is not None and (self.now() - client.last_seen) < PRESENCE_WINDOW

    def forget_quiet_clients(self) -> list:
        gone = self.clients.reap(FORGET_AFTER, self.now())
        for client in gone:
            self.log.info("forgetting %s at %s:%d - nothing for %d minutes",
                          client.name or "an unnamed client", client.endpoint[0],
                          client.endpoint[1], FORGET_AFTER // 60)
        return gone

    def ubi_account_id(self, name: str) -> str:
        """A player's Ubisoft account id, which this server cannot know - nothing ever tells it
        one - so the account name stands in. The pid beside it in every structure that carries
        one is what the client actually matches players by."""
        account = self.accounts.get(name)
        return account.name if account else (name or "")

    def name_for_ubi_account_id(self, account_id: str) -> str:
        account = self.accounts.get(account_id)
        return account.name if account else ""

    def address_for(self, client) -> str:
        """The address to hand *this* client so it can reach us back.

        Never a configured value by default. The address a client is told is the one it can
        actually use, which depends on where it came from: a game on this machine is told
        127.0.0.1, and a game on the network is told whichever of our addresses routes to it -
        which the operating system can answer exactly. The previous attempt took this from a
        setting, and when the server was bound to 0.0.0.0 so that friends could reach it, every
        client was told to connect to "0.0.0.0".
        """
        ip = client.address if hasattr(client, "address") else str(client)
        if ip.startswith("127.") or ip == "::1":
            return "127.0.0.1"
        configured = self.config.get("advertise")
        if configured:
            return configured
        cached = self._advertised.get(ip)
        if cached:
            return cached
        local = "127.0.0.1"
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            # Sends nothing; it only makes the OS pick the route and bind a local address.
            probe.connect((ip, 9))
            local = probe.getsockname()[0]
        except OSError as e:
            self.log.warning("cannot work out which of our addresses reaches %s (%s) - saying"
                             " %s; set 'advertise' if that is wrong", ip, e, local)
        finally:
            probe.close()
        self._advertised[ip] = local
        self.log.info("%s will be told to reach us at %s", ip, local)
        return local

    def secure_url(self, client) -> str:
        """Where the login tells a client the secure server is."""
        return ("prudps:/address=%s;port=%d;CID=1;PID=%d;sid=1;stream=3;type=2"
                % (self.address_for(client), self.secure_port, self.server_pid))

    def station_url(self, client, rvcid: int = None) -> str:
        """The URL handed back when a client registers.

        The address is ours as this client sees it, not the client's own - see services.secure.
        """
        return ("prudps:/address=%s;port=%d;CID=1;PID=%d;sid=1;RVCID=%d;stream=3;type=2"
                % (self.address_for(client), self.game_port, client.pid,
                   rvcid if rvcid is not None else client.rvcid))

    def status(self) -> dict:
        return {
            "uptime": time.time() - self.started,
            "accounts": len(self.accounts),
            "clients": [
                {"name": c.name, "pid": c.pid, "address": "%s:%d" % c.endpoint,
                 "connections": len(c.links), "last_seen": c.last_seen,
                 "urls": list(c.urls),
                 "friends": [{"name": f.name, "pid": f.pid, "relationship": f.relationship,
                              "online": self.is_online(f.pid)}
                             for f in self.friends.contacts(c.name)] if c.name else []}
                for c in self.clients
            ],
            "nat": {"answered": self.nat.answered, "keep_alives": self.nat.keep_alives},
            "packets": {name: {"in": s.received, "out": s.sent}
                        for name, s in self.servers.items()},
        }
