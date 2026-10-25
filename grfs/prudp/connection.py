"""Connection state.

The unit of state is a *connection*, not a player. One running game holds several at once, all
from the same UDP address: its own connection to the authentication server and then to the
secure server (virtual port 15), with a telemetry connection beside each of them (virtual port
14). Each carries its own signature, session id, sequence numbers and fragment buffer.

Keeping that per player instead is the bug this layer exists to prevent. In the previous attempt
one record per address was shared by all of them, so a secure-server CONNECT arriving in the same
second as the telemetry login overwrote the signature the telemetry connection was answering
under; the game silently dropped every reply (wrong signature), resent the same request eleven
times over 28 seconds, gave up, and logged in again a minute later. The same record also held one
reliable sequence counter for every connection and every login, which meant that from a player's
second login onward the first fragmented reply was numbered past the start of the client's
receive window and was never delivered at all.
"""

import time
from collections import OrderedDict

# Answers kept per connection for resending. One request in flight at a time, so this is
# generous; it costs a few kilobytes.
ANSWERS_KEPT = 256


class Link:
    """One PRUDP connection: a virtual port of the game talking to one of our sockets."""

    __slots__ = ("key", "server_port", "vport", "signature", "session", "reliable_seq",
                 "out_seq", "data_seen", "fragments", "last_part", "_answers", "_recording",
                 "created")

    def __init__(self, server_port: int, vport: int):
        self.key = (server_port, vport)
        self.server_port = server_port
        self.vport = vport
        # What the client told us to sign with, from its CONNECT. The game drops anything that
        # does not carry it.
        self.signature = 0
        self.session = 0
        # Our outgoing reliable sequence. The client hands reliable packets to the game strictly
        # in order through a window that starts at 1 for every new connection: one numbered past
        # the next it expects is held back until the gap fills, which never happens.
        self.reliable_seq = 0
        # Our outgoing unreliable sequence. Responses are unreliable (the live servers sent them
        # that way), so this stream is not windowed and the numbering only has to keep rising.
        self.out_seq = 0
        self.data_seen = False
        self.fragments = []
        self.last_part = 0
        self._answers = OrderedDict()
        self._recording = None
        self.created = time.monotonic()

    # ----- sequence numbers -----------------------------------------------------------------
    def next_reliable(self) -> int:
        self.reliable_seq += 1
        return self.reliable_seq

    def next_unreliable(self, after: int = None) -> int:
        """Next unreliable sequence id, at least one past `after` (the request being answered)."""
        if after is not None and after > self.out_seq:
            self.out_seq = after
        self.out_seq += 1
        return self.out_seq

    def saw(self, seq: int) -> None:
        if seq > self.out_seq:
            self.out_seq = seq

    # ----- resending answers ----------------------------------------------------------------
    def answer_to(self, seq: int):
        """Datagrams already sent for reliable packet `seq`, or None if it is new."""
        return self._answers.get(seq)

    def begin_answer(self, seq: int) -> None:
        self._recording = []
        self._answers[seq] = self._recording
        self._answers.move_to_end(seq)
        while len(self._answers) > ANSWERS_KEPT:
            self._answers.popitem(last=False)

    def end_answer(self) -> None:
        self._recording = None

    def record(self, datagram: bytes) -> None:
        if self._recording is not None:
            self._recording.append(datagram)

    def restart(self) -> None:
        """A fresh CONNECT: the game numbers everything from the start again."""
        self._answers.clear()
        self._recording = None
        self.reliable_seq = 0
        self.out_seq = 0
        self.data_seen = False
        self.fragments.clear()
        self.last_part = 0

    def __repr__(self):
        return "<Link %d/%02X sig=%08X session=%02X>" % (self.server_port, self.vport,
                                                         self.signature, self.session)


class AuthState:
    """What one connection got out of logging in.

    Per connection, not per player, because one game logs in twice from the same address: as the
    player on virtual port 15, and as the user "Tracking" - the telemetry connection - on port
    14, each with its own ticket and session key. Sharing one key between them means the secure
    server cannot answer the challenge of whichever logged in first.
    """

    __slots__ = ("name", "pid", "session_key", "ticket")

    def __init__(self, name: str, pid: int, session_key: bytes, ticket: bytes):
        self.name = name
        self.pid = pid
        self.session_key = session_key
        self.ticket = ticket


class Client:
    """One game, identified by the address its packets come from."""

    def __init__(self, cid: int, endpoint):
        # The connection signature we handed out in our SYN reply. The game puts it in every
        # packet it sends us, so it is how a packet is traced back to its sender.
        self.id = cid
        self.endpoint = endpoint
        # The player's identity. The telemetry login deliberately does not touch these: it signs
        # in from the same address under its own name, and letting it write here gave the player
        # the telemetry user's pid for as long as it took them to notice.
        self.pid = 0
        self.rvcid = 0
        self.name = None
        # vport -> AuthState
        self.auth = {}
        # Station URLs the game registered for itself; where other players are told to reach it.
        self.urls = []
        self.links = {}
        self.call_id = 0
        self.last_seen = time.time()
        self.logged_in_at = None
        # Set when the front end asks for the map list, which is the first thing it does once the
        # main menu is actually on screen. A notification pushed before that is answered at the
        # network layer and then dropped - there is no screen to show it on yet.
        self.menu_at = None

    @property
    def address(self) -> str:
        return self.endpoint[0]

    def link(self, server_port: int, vport: int) -> Link:
        key = (server_port, vport)
        link = self.links.get(key)
        if link is None:
            link = Link(server_port, vport)
            self.links[key] = link
        return link

    def find_link(self, server_port: int, vport: int):
        return self.links.get((server_port, vport))

    def signature_for(self, server_port: int, vport: int) -> int:
        """What to sign a packet on this connection with.

        If its CONNECT was never seen - the server restarted under a running game - another
        connection to the same socket stands in: in every capture so far the game has used one
        signature per server, not one per virtual port.
        """
        link = self.links.get((server_port, vport))
        if link is not None and link.signature:
            return link.signature
        for (port, _), other in self.links.items():
            if port == server_port and other.signature:
                return other.signature
        return 0

    def next_call_id(self) -> int:
        self.call_id += 1
        return self.call_id

    def __repr__(self):
        return "<Client %s pid=0x%X %s>" % (self.name or "?", self.pid, self.endpoint)


class ClientTable:
    """Clients by address and by the signature we gave them."""

    # Distinctive in a capture, and the value the live servers used.
    FIRST_ID = 0x12345678

    def __init__(self):
        self._by_endpoint = {}
        self._by_id = {}
        self._next_id = self.FIRST_ID

    def __len__(self):
        return len(self._by_endpoint)

    def __iter__(self):
        return iter(list(self._by_endpoint.values()))

    def for_endpoint(self, endpoint) -> Client:
        client = self._by_endpoint.get(endpoint)
        if client is None:
            client = Client(self._next_id, endpoint)
            self._next_id += 1
            self._by_endpoint[endpoint] = client
            self._by_id[client.id] = client
        return client

    def by_signature(self, signature: int):
        return self._by_id.get(signature)

    def by_pid(self, pid: int):
        """The player with this pid, preferring whoever was heard from most recently.

        There can be more than one record: a game that restarts comes back from a different
        source port, and until the old record is dropped both carry the same pid. Answering with
        the stale one makes a player who is right here look like they left.
        """
        best = None
        for client in self._by_endpoint.values():
            if client.pid == pid and (best is None or client.last_seen > best.last_seen):
                best = client
        return best

    def drop_stale_with_pid(self, keep: Client) -> list:
        """Forget other records for the same player - the ports they used before."""
        gone = [c for c in list(self._by_endpoint.values())
                if c is not keep and c.pid and c.pid == keep.pid]
        for client in gone:
            self.drop(client)
        return gone

    def reap(self, older_than: float, now: float) -> list:
        """Forget records nothing has been heard from for a while."""
        gone = [c for c in list(self._by_endpoint.values()) if now - c.last_seen > older_than]
        for client in gone:
            self.drop(client)
        return gone

    def by_name(self, name: str):
        if not name:
            return None
        for client in self._by_endpoint.values():
            if client.name and client.name.lower() == name.lower():
                return client
        return None

    def drop(self, client: Client) -> None:
        self._by_endpoint.pop(client.endpoint, None)
        self._by_id.pop(client.id, None)
