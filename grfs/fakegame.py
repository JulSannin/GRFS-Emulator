"""A stand-in for the game, over real sockets, for tests and for poking at a running server.

It does what the client does at the PRUDP level and no more than that: one UDP socket per game
with every connection on it told apart by virtual port, the handshake, the ticket, requests
resent until answered, acknowledgements for everything the server marks as needing one, a
receive window for reliable packets that starts at 1, fragments both ways, and an answer to
every call the server makes to it. What it checks on the way is what the game's own transport
checks - a packet signed with the wrong signature is counted, not used, because the game drops
it - and that is the point: a server change that the game would choke on fails here first.

Loss is simulated in-process: `Wire.drop_in` and `Wire.drop_out` take predicates that see each
packet, which is how the tests lose an answer and watch the request being resent.

    wire = Wire()
    one = Player("127.0.0.1", 21030, 21031, "alice", wire=wire)
    one.login()
    one.call("FriendsProtocol", "GetRelationships", {"resultRange": {"m_uiSize": 100}})
"""

import json
import os
import queue
import socket
import threading
import time
import urllib.request

from . import protocols
from .kerberos import PASSWORD_DEFAULT, PASSWORD_TRACKING, TRACKING_NAME
from .prudp import packet as P
from .prudp.crypto import derive_key, hmac_md5, rc4
from .rmc import message
from .rmc.ddl import Schema
from .rmc.stream import Reader, Writer

SCHEMA_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "schema",
                           "grfs-ddl.json")

PLAYER_VPORT = 0x3F        # stream 3, port 15: the player's own connection
TELEMETRY_VPORT = 0x3E     # stream 3, port 14: the telemetry connection beside it
SERVER_VPORT = 0x31        # stream 3, port 1: the server's end

# How long to wait for an answer before sending the request again. The game's own figure on a
# LAN is a few tens of milliseconds; this is slower so a busy test machine does not resend for
# nothing, and still fast enough that a lost packet costs a test a fraction of a second.
RESEND_AFTER = 0.25
TIMEOUT = 5.0

PROTOCOL_IDS = {name: pid for pid, name in protocols.NAMES.items()}

_schema = None


def schema() -> Schema:
    """The protocol schema, loaded once: the same DDL file the server reads, and the layouts
    every service declares for the protocols the DDL does not have (Hermes, Overlord). Those
    were read out of the client's own stubs, so the real game has them compiled in; the fake
    one takes the very same declarations, never a second copy that could drift."""
    global _schema
    if _schema is None:
        from .rmc.router import declare
        from .services import all_services
        loaded = Schema().load_ddl_json(SCHEMA_PATH)
        for service in all_services():
            declare(loaded, service)
            PROTOCOL_IDS.setdefault(service.protocol_name, service.protocol_id)
        _schema = loaded
    return _schema


class RmcFailure(Exception):
    """The server answered a call with an error code."""

    def __init__(self, protocol: str, method: str, code: int):
        super().__init__("%s.%s failed with 0x%08X" % (protocol, method, code))
        self.protocol = protocol
        self.method = method
        self.code = code


class Wire:
    """One UDP socket, as one running game has, and a thread reading it.

    Packets are handed to the connection they belong to by (server address, our virtual port).
    Anything else - a NAT answer, which is a bare datagram, or a packet for no connection - goes
    to `raw` for whoever is interested.
    """

    def __init__(self, bind: str = "127.0.0.1", port: int = 0):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind((bind, port))
        self.sock.settimeout(0.1)
        self.address = self.sock.getsockname()
        self.connections = {}
        self.raw = queue.Queue()
        self.drop_in = []          # predicates on a parsed packet arriving: True loses it
        self.drop_out = []         # the same for packets leaving
        self.dropped_in = 0
        self.dropped_out = 0
        self.received = 0
        self.sent = 0
        self.history = []          # (endpoint, datagram) of everything that arrived
        self.errors = []
        self.closed = False
        self.thread = threading.Thread(target=self._read, name="wire %s:%d" % self.address,
                                       daemon=True)
        self.thread.start()

    @property
    def port(self) -> int:
        return self.address[1]

    def close(self) -> None:
        self.closed = True
        self.thread.join(2)
        self.sock.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ----- out ------------------------------------------------------------------------------
    def send(self, pkt: P.Packet, endpoint) -> None:
        if any(rule(pkt) for rule in list(self.drop_out)):
            self.dropped_out += 1
            return
        self.send_bytes(pkt.encode(), endpoint)

    def send_bytes(self, data: bytes, endpoint) -> None:
        self.sent += 1
        self.sock.sendto(data, endpoint)

    # ----- in -------------------------------------------------------------------------------
    def _read(self) -> None:
        while not self.closed:
            try:
                data, endpoint = self.sock.recvfrom(65535)
            except socket.timeout:
                continue
            except ConnectionResetError:
                # Windows reports an earlier datagram's "port unreachable" on the next read.
                continue
            except OSError:
                if self.closed:
                    return
                raise
            self.received += 1
            self.history.append((endpoint, data))
            try:
                self._deliver(data, endpoint)
            except Exception as e:                      # a test must see this, not lose it
                self.errors.append("%s from %s: %r" % (type(e).__name__, endpoint, e))

    def _deliver(self, data: bytes, endpoint) -> None:
        if data[:1] == b"\xA5":
            self.raw.put((data, endpoint))
            return
        try:
            packets = P.parse(data)
        except P.PacketError:
            self.raw.put((data, endpoint))
            return
        for pkt in packets:
            if any(rule(pkt) for rule in list(self.drop_in)):
                self.dropped_in += 1
                continue
            connection = self.connections.get((endpoint, pkt.dst))
            if connection is None:
                self.raw.put((data, endpoint))
                continue
            connection.on_packet(pkt)


class Connection:
    """One PRUDP connection from a virtual port of ours to one server."""

    def __init__(self, wire: Wire, endpoint, vport: int = PLAYER_VPORT,
                 signature: int = None, session: int = 0x52):
        self.wire = wire
        self.endpoint = endpoint
        self.vport = vport
        # What the server is to sign with when it writes to us. The game makes one up per
        # connection; this does too, so that a reply signed for the wrong one shows.
        self.signature = signature if signature is not None else (
            0x7F000000 | (wire.port << 8) | vport) & 0xFFFFFFFF
        self.server_signature = 0
        self.session = session
        self.seq = 0
        self.seq_lock = threading.Lock()    # answers to the server's calls number from here too
        self.call_id = 0
        self.lock = threading.Condition()
        self.syn_reply = None
        self.connect_reply = None
        self.acked = set()
        self.pings = 0
        self.disconnected = False
        self.responses = {}
        self.requests = queue.Queue()       # calls the server made to us, already answered
        self.answer_requests = True
        self.bad_signatures = 0
        self.expected = 1                   # next reliable sequence id we may hand on
        self.held = {}
        self.fragments = []
        self.duplicates = 0
        self.log = []                       # (direction, description) of every packet
        wire.connections[(endpoint, vport)] = self

    def close(self) -> None:
        self.wire.connections.pop((self.endpoint, self.vport), None)

    # ----- handshake ------------------------------------------------------------------------
    def handshake(self, timeout: float = TIMEOUT) -> int:
        with self.lock:
            self.syn_reply = self.connect_reply = None
            self.acked.clear()
            self.expected = 1
            self.held.clear()
            self.fragments.clear()
        syn = P.Packet(self.vport, SERVER_VPORT, P.SYN, P.FLAG_NEED_ACK, 0, 0, 0)
        reply = self._exchange(syn, lambda: self.syn_reply, timeout, "SYN")
        self.server_signature = reply.conn_sig
        self.seq = 1
        connect = P.Packet(self.vport, SERVER_VPORT, P.CONNECT,
                           P.FLAG_RELIABLE | P.FLAG_NEED_ACK, self.session,
                           self.server_signature, self.seq, conn_sig=self.signature)
        self._exchange(connect, lambda: self.connect_reply, timeout, "CONNECT")
        return self.server_signature

    def connect_with_ticket(self, ticket: bytes, session_key: bytes, code: int = 0xE2450886,
                            timeout: float = TIMEOUT) -> int:
        """The secure server's CONNECT: the ticket, and a number only its owner can read. Returns
        what the server made of the number, which the game expects to be one more."""
        sealed = rc4(session_key, Writer().u32(0).u32(0).u32(code).bytes())
        payload = Writer().buffer(ticket).buffer(sealed + hmac_md5(session_key, sealed)).bytes()
        with self.lock:
            self.connect_reply = None
        self.seq = 1
        connect = P.Packet(self.vport, SERVER_VPORT, P.CONNECT,
                           P.FLAG_RELIABLE | P.FLAG_NEED_ACK, self.session,
                           self.server_signature, self.seq, conn_sig=self.signature,
                           payload=P.wrap_payload(P.STREAM_RVSEC, payload))
        reply = self._exchange(connect, lambda: self.connect_reply, timeout, "CONNECT")
        if not reply.payload:
            raise ValueError("the secure server did not answer the challenge")
        r = Reader(P.unwrap_payload(P.STREAM_RVSEC, reply.payload))
        if r.u32() != 4:
            raise ValueError("the challenge answer is not four bytes long")
        return r.u32()

    def _exchange(self, pkt, ready, timeout, what):
        deadline = time.monotonic() + timeout
        while True:
            self._send(pkt)
            with self.lock:
                self.lock.wait_for(ready, min(RESEND_AFTER, max(0.0, deadline - time.monotonic())))
                got = ready()
            if got is not None:
                return got
            if time.monotonic() >= deadline:
                raise TimeoutError("no answer to %s from %s:%d" % ((what,) + self.endpoint))

    # ----- calls ----------------------------------------------------------------------------
    def call(self, protocol: int, method: int, params: bytes, timeout: float = TIMEOUT,
             reliable: bool = True) -> message.Message:
        """One request, resent until its answer arrives - the answer may come in fragments."""
        self.call_id += 1
        call_id = self.call_id
        body = message.request(protocol, call_id, method, params)
        packets = self._data_packets(body, reliable)
        deadline = time.monotonic() + timeout
        while True:
            # Resent until answered, but only what the server has not acknowledged - the game
            # does not resend a fragment the server already has.
            with self.lock:
                unacked = [pkt for pkt in packets if pkt.seq not in self.acked]
            for pkt in unacked:
                self._send(pkt)
            with self.lock:
                self.lock.wait_for(lambda: call_id in self.responses,
                                   min(RESEND_AFTER, max(0.0, deadline - time.monotonic())))
                if call_id in self.responses:
                    return self.responses.pop(call_id)
            if time.monotonic() >= deadline:
                raise TimeoutError("no answer to %s method %d from %s:%d"
                                   % ((protocols.NAMES.get(protocol, hex(protocol)), method)
                                      + self.endpoint))

    def send_message(self, body: bytes, reliable: bool = True) -> list:
        """Put an RMC message on the wire once, without waiting. Returns the packets sent."""
        packets = self._data_packets(body, reliable)
        for pkt in packets:
            self._send(pkt)
        return packets

    def _data_packets(self, body: bytes, reliable: bool) -> list:
        chunks = [body[i:i + P.FRAGMENT_SIZE] for i in range(0, len(body), P.FRAGMENT_SIZE)]
        flags = P.FLAG_NEED_ACK | (P.FLAG_RELIABLE if reliable else 0)
        out = []
        for n, chunk in enumerate(chunks, 1):
            out.append(P.Packet(self.vport, SERVER_VPORT, P.DATA, flags, self.session,
                                self.server_signature, self.next_seq(),
                                part=0 if n == len(chunks) else n,
                                payload=P.wrap_payload(P.stream_of(self.vport), chunk)))
        return out

    def next_seq(self) -> int:
        with self.seq_lock:
            self.seq += 1
            return self.seq

    def ping(self, timeout: float = TIMEOUT) -> None:
        before = self.pings
        pkt = P.Packet(self.vport, SERVER_VPORT, P.PING, P.FLAG_NEED_ACK, self.session,
                       self.server_signature, self.next_seq())
        self._exchange(pkt, lambda: True if self.pings > before else None, timeout, "PING")

    def disconnect(self, timeout: float = TIMEOUT) -> None:
        self.disconnected = False
        pkt = P.Packet(self.vport, SERVER_VPORT, P.DISCONNECT,
                       P.FLAG_RELIABLE | P.FLAG_NEED_ACK, self.session, self.server_signature,
                       self.next_seq())
        self._exchange(pkt, lambda: True if self.disconnected else None, timeout, "DISCONNECT")

    def _send(self, pkt: P.Packet) -> None:
        self.log.append(("->", pkt.describe()))
        self.wire.send(pkt, self.endpoint)

    # ----- what arrives (on the wire's thread) ----------------------------------------------
    def on_packet(self, pkt: P.Packet) -> None:
        self.log.append(("<-", pkt.describe()))
        with self.lock:
            if pkt.type == P.SYN:
                if pkt.has(P.FLAG_ACK):
                    self.syn_reply = pkt
            elif pkt.signature != self.signature:
                # The game drops these without a word, which is exactly why they are counted.
                self.bad_signatures += 1
            elif pkt.type == P.CONNECT:
                if pkt.has(P.FLAG_ACK):
                    self.connect_reply = pkt
            elif pkt.type == P.PING:
                if pkt.has(P.FLAG_ACK):
                    self.pings += 1
            elif pkt.type == P.DISCONNECT:
                if pkt.has(P.FLAG_ACK):
                    self.disconnected = True
            elif pkt.type == P.DATA:
                self._on_data(pkt)
            self.lock.notify_all()

    def _on_data(self, pkt: P.Packet) -> None:
        if pkt.has(P.FLAG_ACK):
            self.acked.add(pkt.seq)
            return
        if pkt.has(P.FLAG_NEED_ACK):
            self.wire.send(P.Packet(self.vport, pkt.src, P.DATA, P.FLAG_ACK, self.session,
                                    self.server_signature, pkt.seq, part=pkt.part), self.endpoint)
        if not pkt.has(P.FLAG_RELIABLE):
            self._take(pkt)
            return
        # Reliable packets go on strictly in order from 1, as the game's window releases them.
        if pkt.seq < self.expected or pkt.seq in self.held:
            self.duplicates += 1
            return
        self.held[pkt.seq] = pkt
        while self.expected in self.held:
            self._take(self.held.pop(self.expected))
            self.expected += 1

    def stuck(self) -> bool:
        """Whether reliable packets are waiting behind a gap that never filled - the state that
        loses every long reply."""
        with self.lock:
            return bool(self.held)

    def _take(self, pkt: P.Packet) -> None:
        plain = P.unwrap_payload(pkt.stream, pkt.payload)
        if pkt.part != 0:
            self.fragments.append(plain)
            return
        if self.fragments:
            plain = b"".join(self.fragments) + plain
            self.fragments.clear()
        msg = message.parse(plain)
        if not msg.is_request:
            self.responses[msg.call_id] = msg
            return
        self.requests.put(msg)
        if self.answer_requests:
            # Every call the server makes here is answered, as the game answers them: a reply of
            # nothing at all is right for the notifications, which declare no results.
            body = message.response(msg.protocol, msg.call_id, msg.method, b"")
            for out in self._data_packets(body, True):
                self.wire.send(out, self.endpoint)


class Player:
    """One game, signed in: both connections on one socket, and calls by protocol and method.

    The credentials it sends are made up and distinctive, so that a test can look for them in
    the server's log and data files and be sure they are in neither.
    """

    PASSWORD = "not-a-real-password-7c1d"
    ONLINE_KEY = "NOT-A-REAL-KEY-7c1d"

    def __init__(self, host: str, auth_port: int, secure_port: int, name: str,
                 wire: Wire = None, urls: list = None, p2p_port: int = 2347):
        self.host = host
        self.auth_port = auth_port
        self.secure_port = secure_port
        self.name = name
        self.own_wire = wire is None
        # A socket on the loopback cannot reach another machine, and one on every interface
        # says "0.0.0.0" when asked its address, so which it is depends on where the server is.
        self.wire = wire or Wire(bind="127.0.0.1" if host.startswith("127.") else "0.0.0.0")
        self.schema = schema()
        self.urls = urls
        self.p2p_port = p2p_port
        self.pid = 0
        self.ticket = b""
        self.session_key = b""
        self.secure_url = ""
        self.server_pid = 0
        self.requested_ticket = b""
        self.rvcid = 0
        self.login_reply = None
        self.register_reply = None
        self.challenge = None
        self.auth = None
        self.secure = None
        self.telemetry = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self) -> None:
        for c in (self.auth, self.secure, self.telemetry):
            if c is not None:
                c.close()
        if self.own_wire:
            self.wire.close()

    # ----- signing in -----------------------------------------------------------------------
    def login(self, register: bool = True) -> "Player":
        """The game's path: log in, ask for the ticket, prove it on the secure server, register."""
        self.auth = Connection(self.wire, (self.host, self.auth_port), PLAYER_VPORT)
        self.auth.handshake()
        reply = self._call(self.auth, "TicketGrantingProtocol", "LoginEx", {
            "strUserName": self.name, "oExtraData": self.custom_data()})
        self.login_reply = reply
        self.pid = reply["pidPrincipal"]
        self.ticket = reply["pbufResponse"]
        self.secure_url = reply["pConnectionData"]["m_urlRegularProtocols"]
        self.session_key, self.server_pid, _ = read_ticket(self.ticket, self.pid,
                                                           PASSWORD_DEFAULT)
        again = self._call(self.auth, "TicketGrantingProtocol", "RequestTicket",
                           {"idSource": self.pid, "idTarget": self.server_pid})
        self.requested_ticket = again["bufResponse"]

        host, port = url_address(self.secure_url)
        self.secure = Connection(self.wire, (self.host if host == "127.0.0.1" else host, port),
                                 PLAYER_VPORT)
        self.secure.handshake()
        code = 0x13572468
        self.challenge = (code, self.secure.connect_with_ticket(self.ticket, self.session_key,
                                                                code))
        if self.challenge[1] != code + 1:
            raise ValueError("the secure server answered the challenge with %08X, not %08X"
                             % (self.challenge[1], code + 1))
        if register:
            self.register()
        return self

    def register(self, urls: list = None) -> dict:
        if urls is None:
            urls = self.urls or ["prudp:/address=%s;port=%d;sid=15;type=2"
                                 % (local_address(self.host), self.p2p_port)]
        self.register_reply = self.call("SecureConnectionProtocol", "RegisterEx", {
            "vecMyURLs": urls, "hCustomData": self.custom_data()})
        self.rvcid = self.register_reply["pidConnectionID"]
        return self.register_reply

    def login_telemetry(self) -> int:
        """The second login a game makes from the same socket: user "Tracking", plain Login, on
        the port beside the player's. Returns the telemetry pid."""
        auth = Connection(self.wire, (self.host, self.auth_port), TELEMETRY_VPORT)
        auth.handshake()
        reply = self._call(auth, "TicketGrantingProtocol", "Login", {"strUserName": TRACKING_NAME})
        pid, ticket = reply["pidPrincipal"], reply["pbufResponse"]
        key, _, _ = read_ticket(ticket, pid, PASSWORD_TRACKING)
        secure = Connection(self.wire, (self.host, self.secure_port), TELEMETRY_VPORT)
        secure.handshake()
        code = 0x2468ACE0
        if secure.connect_with_ticket(ticket, key, code) != code + 1:
            raise ValueError("the telemetry connection's challenge was answered wrong")
        self.telemetry = secure
        self._call(secure, "SecureConnectionProtocol", "RegisterEx", {
            "vecMyURLs": ["prudp:/address=10.99.99.99;port=1;sid=14;type=2"],
            "hCustomData": {"__class__": "UbiAuthenticationLoginCustomData",
                            "m_userName": TRACKING_NAME, "m_onlineKey": "", "m_password": ""}})
        auth.close()
        return pid

    def custom_data(self) -> dict:
        return {"__class__": "UbiAuthenticationLoginCustomData", "m_userName": self.name,
                "m_onlineKey": self.ONLINE_KEY, "m_password": self.PASSWORD}

    # ----- calls ----------------------------------------------------------------------------
    def call(self, protocol: str, method, args: dict = None, timeout: float = TIMEOUT) -> dict:
        """A call on the secure server by protocol name and method name or id, decoded."""
        return self._call(self.secure, protocol, method, args, timeout)

    def call_raw(self, protocol, method_id: int, params: bytes = b"",
                 timeout: float = TIMEOUT) -> message.Message:
        """A call the schema need not know about. Returns the message, success or not."""
        pid = protocol if isinstance(protocol, int) else PROTOCOL_IDS[protocol]
        return self.secure.call(pid, method_id, params, timeout)

    def _call(self, connection, protocol: str, method, args=None, timeout=TIMEOUT) -> dict:
        mdef = find_method(self.schema, protocol, method)
        reply = connection.call(PROTOCOL_IDS[protocol], mdef.id,
                                self.schema.encode_request(mdef, args or {}), timeout)
        if not reply.success:
            raise RmcFailure(protocol, mdef.name, reply.error)
        if reply.method != mdef.id:
            raise ValueError("asked %s.%s, the answer is for method %d"
                             % (protocol, mdef.name, reply.method))
        return self.schema.decode_response(mdef, reply.body)

    def next_server_call(self, timeout: float = TIMEOUT) -> tuple:
        """(protocol, method, arguments) of the next call the server made to this game - a
        notification, say - waiting for one if need be. It has already been answered."""
        msg = self.secure.requests.get(timeout=timeout)
        name = protocols.NAMES.get(msg.protocol, "")
        mdef = self.schema.method(name, msg.method)
        args = self.schema.decode_request(mdef, msg.body) if mdef else {"__raw__": msg.body}
        return name, mdef.name if mdef else msg.method, args

    # ----- the friends screen, the calls the tests make most --------------------------------
    def relationships(self) -> dict:
        """name -> (relationship, online) from GetRelationships, as the contact list shows it."""
        answer = self.call("FriendsProtocol", "GetRelationships",
                           {"resultRange": {"m_uiOffset": 0, "m_uiSize": 200}})
        if answer["uiTotalCount"] != len(answer["lstRelationshipsList"]):
            raise ValueError("total %d but %d entries" % (answer["uiTotalCount"],
                                                          len(answer["lstRelationshipsList"])))
        return {r["m_strName"]: (r["m_byRelationship"], bool(r["m_byStatus"]))
                for r in answer["lstRelationshipsList"]}


class Peer:
    """The game's other socket - the peer-to-peer one, which never handshakes with the server
    and asks it only one thing: what address it is seen from."""

    def __init__(self, bind: str = "127.0.0.1", port: int = 0):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind((bind, port))
        self.sock.settimeout(RESEND_AFTER)
        self.address = self.sock.getsockname()

    def close(self) -> None:
        self.sock.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def probe(self, host: str, port: int, kind: int = 0x02, wrapped: bool = True,
              tries: int = 8):
        """Ask for our public URL; returns it, or None if nothing came back. The game sends this
        four times a second, so resending is what it does too."""
        payload = bytes([0xA5, kind])
        data = (P.Packet(0x71, 0x71, P.USER, 0, 0, 0, 0, payload=payload).encode()
                if wrapped else payload)
        for _ in range(tries):
            self.sock.sendto(data, (host, port))
            try:
                answer, _ = self.sock.recvfrom(65535)
            except socket.timeout:
                continue
            except ConnectionResetError:
                continue
            if answer[:2] != bytes([0xA5, 0x02]):
                raise ValueError("the answer does not start with A5 02: %s" % answer[:16].hex(" "))
            return answer[2:].rstrip(b"\x00").decode("latin-1")
        return None


# ----- helpers ------------------------------------------------------------------------------
def find_method(s: Schema, protocol: str, method):
    proto = s.protocols.get(protocol)
    if proto is None:
        raise KeyError("no protocol %s in the schema" % protocol)
    if isinstance(method, int):
        mdef = proto.methods.get(method)
    else:
        mdef = next((m for m in proto.methods.values() if m.name == method), None)
    if mdef is None:
        raise KeyError("%s has no method %s" % (protocol, method))
    return mdef


def read_ticket(blob: bytes, pid: int, password: bytes):
    """Undo what the server did to the ticket, exactly as the client does: the session key, the
    server's pid, and the opaque part."""
    key = derive_key(pid, password)
    body, mac = blob[:-16], blob[-16:]
    if hmac_md5(key, body) != mac:
        raise ValueError("the ticket's HMAC does not check out - wrong password or pid")
    r = Reader(rc4(key, body))
    return r.take(16), r.u32(), r.buffer()


def game_address():
    """(machine name, address) the game binds its P2P socket, UDP 2346, to.

    Not 0.0.0.0 and not the address that routes to the server: the game takes the first address
    its machine name resolves to (5DF260 - gethostname, gethostbyname, the first entry) and binds
    exactly that (5EBFF0). On a machine with a VPN adapter first in that list the game sits on the
    VPN's address, and Windows will not send from a socket bound to one interface to an address
    on another - so the game's NAT probe to a server on 127.0.0.1 never leaves.

    The environment variable _CLUSTER_NETWORK_NAME_ replaces the machine name for one process,
    for the game as for this; set to an address, it is that address (tools/launch_game.py).
    The winsock calls are made directly, the way the game makes them, because Python's own
    gethostname reads the name another way and does not see that variable.
    """
    name = socket.gethostname()
    if os.name == "nt":
        import ctypes
        ws2 = ctypes.WinDLL("ws2_32")
        buf = ctypes.create_string_buffer(256)
        if ws2.gethostname(buf, len(buf)) == 0:
            name = buf.value.decode("ascii", "replace")

        class HOSTENT(ctypes.Structure):
            _fields_ = [("h_name", ctypes.c_char_p), ("h_aliases", ctypes.c_void_p),
                        ("h_addrtype", ctypes.c_short), ("h_length", ctypes.c_short),
                        ("h_addr_list", ctypes.POINTER(ctypes.POINTER(ctypes.c_ubyte * 4)))]
        ws2.gethostbyname.restype = ctypes.POINTER(HOSTENT)
        ws2.gethostbyname.argtypes = [ctypes.c_char_p]
        found = ws2.gethostbyname(name.encode("ascii", "replace"))
        if found and found.contents.h_addrtype == socket.AF_INET and found.contents.h_addr_list \
                and found.contents.h_addr_list[0]:
            return name, socket.inet_ntoa(bytes(found.contents.h_addr_list[0].contents))
        return name, None
    try:
        return name, socket.gethostbyname(name)
    except OSError:
        return name, None


def local_address(host: str) -> str:
    """Which of this machine's addresses reaches `host` - what a game registers as its own."""
    if host.startswith("127."):
        return "127.0.0.1"
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect((host, 9))                 # sends nothing; only picks the route
        return probe.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        probe.close()


def url_address(url: str):
    """(address, port) out of a station URL such as prudps:/address=1.2.3.4;port=21031;..."""
    fields = dict(part.split("=", 1) for part in url.split(":/", 1)[1].split(";") if "=" in part)
    return fields["address"], int(fields["port"])


def fetch_config(host: str, port: int, config_id: str = "23ad683803a0457cabce83f905811dbc",
                 path: str = "/OnlineConfigService.svc/GetOnlineConfig", every: bool = False):
    """The configuration service's answer as {name: value}, asked for the way the game asks.
    Most keys have one value; with `every`, each name maps to the list of all of them - which
    punch_DetectUrls needs, since the game wants two."""
    url = "http://%s:%d%s?onlineConfigID=%s&target=client" % (host, port, path, config_id)
    request = urllib.request.Request(url, headers={"Host": "onlineconfigservice.ubi.com",
                                                   "Accept": "*/*"})
    with urllib.request.urlopen(request, timeout=5) as response:
        rows = json.loads(response.read().decode("utf-8"))
    return {row["Name"]: list(row["Values"]) if every else row["Values"][0] for row in rows}
