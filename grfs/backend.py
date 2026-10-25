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
from .sessions import Sessions

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

# The game's own connection to a server: stream 3 (RV secure), port 15. Telemetry is port 14.
PLAYER_VPORT = 0x3F


class Backend:
    def __init__(self, config: Config = None, schema_path: str = None, data_dir: str = None):
        self.config = config or Config()
        self.log = logs.get("backend")
        self.started = time.time()
        self.schema = Schema().load_ddl_json(schema_path or SCHEMA_PATH)
        self.protocol_names = dict(protocols.NAMES)
        if data_dir is None:
            data_dir = self.config.get("data_dir") or DATA_DIR
            if not os.path.isabs(data_dir):
                data_dir = os.path.join(ROOT, data_dir)
        self.data_dir = data_dir
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
        # Open game sessions and invitations. Kept in memory only: each one points at a running
        # game, and none of them is still running after a restart.
        self.sessions = Sessions(now=self.now)
        self.router = Router(self)
        self.nat = NatEcho(self)
        self.server_pid = SERVER_PID
        self.auth_port = self.config.int("auth_port", 21030)
        self.secure_port = self.config.int("secure_port", 21031)
        self.game_port = self.config.int("game_port", 2347)
        self._advertised = {}
        self.servers = {}
        # Anything else that keeps state on disk - clans, statistics - adds itself here with
        # add_store, and is saved with the accounts when the server stops.
        self.stores = []
        self.install_services()

    def install_services(self) -> None:
        from .services import all_services
        for factory in all_services():
            service = self.router.add(factory(self))
            self.log.debug("%s (0x%02X): %s", service.protocol_name, service.protocol_id,
                           ", ".join(name for _, name in service.handled()) or "nothing yet")

    # ----- state on disk --------------------------------------------------------------------
    def data_path(self, filename: str):
        """Where a store keeps its file, or None when this server keeps nothing on disk."""
        return os.path.join(self.data_dir, filename) if self.data_dir else None

    def add_store(self, store):
        """Register something with a save() to be called when the server stops."""
        self.stores.append(store)
        return store

    def save(self) -> None:
        for store in [self.accounts, self.friends] + self.stores:
            store.save()

    # ----- telling a player something -------------------------------------------------------
    def notify(self, pid: int, event_type: int, source: int = 0, param1: int = 0,
               param2: int = 0, text: str = "", param3: int = 0) -> bool:
        """Send one NotificationEvent to a player's own connection on the secure server.

        Fire and forget, as the live servers did: the client answers, but nothing in the answer
        matters. Returns whether the player was there to be told.
        """
        client = self.clients.by_pid(pid)
        server = self.servers.get("secure")
        if client is None or server is None:
            return False
        link = client.find_link(self.secure_port, PLAYER_VPORT)
        if link is None:
            return False
        mdef = self.schema.method("NotificationProtocol", 1)
        body = self.schema.encode_request(mdef, {"oEvent": {
            "m_pidSource": source, "m_uiType": event_type, "m_uiParam1": param1,
            "m_uiParam2": param2, "m_strParam": text, "m_uiParam3": param3}})
        server.send_request(client, link, protocols.NOTIFICATION, 1, body)
        self.log.debug("told %s: notification %d (%d, %d, %r, %d) from 0x%X",
                       client.name or client.address, event_type, param1, param2, text, param3,
                       source)
        return True

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

    def notice_departures(self) -> list:
        """Tell the friends of every player who went quiet since the last look, and of every
        one who came back.

        A friend's screen shows a player online until something makes it refetch, and nothing
        else would: the client has no presence notification, and leaving a game sends nothing
        either - a DISCONNECT is part of a normal login (see net). Silence past the presence
        window is the sign-out, so that is when the friends are told, with the same "friend
        changed" notification a sign-in sends (services.secure.tell_friends). Not while the same
        player is still here from another record. Returns the clients that went away."""
        secure = self.router.service_for(protocols.SECURE_CONNECTION)
        now = self.now()
        went = []
        for client in self.clients:
            if not client.pid or not client.name:
                continue
            quiet = now - client.last_seen >= PRESENCE_WINDOW
            if quiet == client.away:
                continue
            client.away = quiet
            if quiet and self.is_online(client.pid):
                continue                     # another record of the same player is live
            if quiet:
                went.append(client)
            if secure is not None:
                secure.tell_friends(client)
        return went

    def forget_quiet_clients(self) -> list:
        gone = self.clients.reap(FORGET_AFTER, self.now())
        for client in gone:
            self.log.info("forgetting %s at %s:%d - nothing for %d minutes",
                          client.name or "an unnamed client", client.endpoint[0],
                          client.endpoint[1], FORGET_AFTER // 60)
            # A game that is gone is out of every session it was in, as if it had left each one:
            # a session it hosted waits for one of the others to take over, and one nobody is
            # left in goes. Not while the same player is still here from another port.
            if client.pid and self.clients.by_pid(client.pid) is None:
                for session in self.sessions.forget(client.pid):
                    self.log.info("%s is out of session %d", client.name or "pid 0x%X"
                                  % client.pid, session.id)
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
                 "urls": list(c.urls), "away": c.away,
                 "friends": [{"name": f.name, "pid": f.pid, "relationship": f.relationship,
                              "online": self.is_online(f.pid)}
                             for f in self.friends.contacts(c.name)] if c.name else [],
                 "blocked": [f.name for f in self.friends.blacklist(c.name)] if c.name else []}
                for c in self.clients
            ],
            "sessions": self.sessions.status(self.accounts.name_for),
            "nat": {"answered": self.nat.answered, "keep_alives": self.nat.keep_alives,
                    "told_open": getattr(self.servers.get("punch"), "resolved", 0)},
            "packets": {name: {"in": s.received, "out": s.sent}
                        for name, s in self.servers.items()},
        }
