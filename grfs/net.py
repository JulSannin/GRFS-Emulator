"""The PRUDP endpoint: one UDP socket, the handshake, acknowledgements, fragments, and the way
up into RMC.

Rules this layer keeps, each of them paid for by a bug in the previous attempt:

* A reliable packet is acknowledged the moment it arrives, not once its answer is ready. The
  client waits tens of milliseconds on a LAN before resending, and a database lookup is longer
  than that.
* A reliable packet that arrives twice is answered with the bytes its first copy was answered
  with, and is not handled again. Handling the copies created duplicate sessions that every
  later search then offered, and spliced resent fragments into a fourth, garbage message.
* Responses go out unreliable, the way the live servers sent them. Fragments go out reliable and
  numbered from 1 within their own connection, because the client releases reliable packets to
  the game strictly in order through a window that starts there.
* Replies are signed with the signature of the connection the request came in on, and leave by
  the socket it arrived on.
* On Windows a UDP socket is told not to report "port unreachable" (ignore_port_unreachable).
  Every game that is closed leaves a port behind that the server still writes to for a while -
  a resend, a notification, a ping answer - and Windows hands the ICMP answer to the next read
  as an error. asyncio's Windows loop (before Python 3.12) stops reading a datagram socket for
  good after such an error: the whole secure server went deaf to every player, and only a
  busy moment was needed for it to happen.
"""

import asyncio
import ctypes
import struct
import sys

from . import log as logs
from .prudp import packet as P
from .prudp.crypto import rc4
from .rmc import message
from .rmc.router import Errors
from .rmc.stream import Reader, Writer


# Fragments kept per connection while waiting for the rest of their message. Most of what the
# game sends is a few fragments long, but a first statistics upload carries every dirty item row
# of a new profile in one SendPlayerStatsInt32, and one blob stat may be 64 KB: a cap of 64
# (about 61 KB) would throw such a message away half-built, every time it was resent. 512 is
# about 490 KB, and still bounds what a broken sender can leave behind.
FRAGMENTS_KEPT = 512


# WSAIoctl's SIO_UDP_CONNRESET, _WSAIOW(IOC_VENDOR, 12); socket.ioctl does not take it.
SIO_UDP_CONNRESET = 0x9800000C


def ignore_port_unreachable(transport) -> bool:
    """Switch off Windows' "port unreachable" errors on a datagram transport's socket (see the
    module docstring). True when that was done or is not needed, False when Windows refused."""
    if sys.platform != "win32":
        return True
    sock = transport.get_extra_info("socket")
    if sock is None:
        return False
    ws2 = ctypes.WinDLL("ws2_32")
    off = ctypes.c_uint32(0)
    returned = ctypes.c_uint32(0)
    ws2.WSAIoctl.argtypes = (ctypes.c_size_t, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_uint32,
                             ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_void_p,
                             ctypes.c_void_p)
    return ws2.WSAIoctl(sock.fileno(), SIO_UDP_CONNRESET, ctypes.byref(off), ctypes.sizeof(off),
                        None, 0, ctypes.byref(returned), None, None) == 0


def _whole_message(plain: bytes) -> bool:
    """Whether these bytes start with an RMC size that accounts for exactly all of them."""
    return len(plain) >= 4 and struct.unpack_from("<I", plain)[0] == len(plain) - 4


def _fragment_run(fragments: dict, end: int):
    """The sequence ids of the message ending at `end`, front to back, if all of it is here."""
    run = [end]
    expected = None
    seq = end - 1
    while seq in fragments:
        part = fragments[seq][0]
        if part == 0 or (expected is not None and part != expected):
            return None
        run.append(seq)
        if part == 1:
            return run[::-1]
        expected = part - 1
        seq -= 1
    return None


class PrudpServer(asyncio.DatagramProtocol):
    def __init__(self, backend, name: str, port: int):
        self.backend = backend
        self.name = name
        self.port = port
        self.transport = None
        self.log = logs.get(name)
        self.sent = 0
        self.received = 0
        self.unplaced = set()

    # ----- socket ---------------------------------------------------------------------------
    def connection_made(self, transport):
        self.transport = transport
        if not ignore_port_unreachable(transport):
            self.log.warning("could not switch off port-unreachable errors on UDP %d: a game"
                             " that quits may stop this socket from reading", self.port)

    def error_received(self, exc):
        self.log.warning("socket error on UDP %d: %s", self.port, exc)

    def sendto(self, data: bytes, endpoint) -> None:
        self.sent += 1
        if self.transport is not None:
            self.transport.sendto(data, endpoint)

    def datagram_received(self, data: bytes, endpoint) -> None:
        self.received += 1
        try:
            self.handle_datagram(data, endpoint)
        except Exception:
            self.log.exception("while handling %d bytes from %s: %s", len(data), endpoint,
                               logs.hexs(data))

    # ----- packets --------------------------------------------------------------------------
    def handle_datagram(self, data: bytes, endpoint) -> None:
        # The NAT echo probe a peer's game socket sends is the one thing here that is not
        # necessarily well-formed PRUDP, and its answer is a bare datagram. Give it first refusal.
        if self.backend.nat.looks_like_probe(data):
            self.backend.nat.handle(self, data, endpoint)
            return
        try:
            packets = P.parse(data)
        except P.PacketError as e:
            self.log.warning("from %s: %s (%s)", endpoint, e, logs.hexs(data))
            return
        for pkt in packets:
            self.log.trace("<- %s %s", pkt.describe(), endpoint)
            self.handle_packet(pkt, endpoint)

    def handle_packet(self, pkt: P.Packet, endpoint) -> None:
        if pkt.type == P.SYN:
            self.handle_syn(pkt, endpoint)
            return

        # A NAT probe belongs to no connection and never will: it comes from the game's
        # peer-to-peer socket, which has not handshaked with anything, so it has no signature to
        # be found by. Answering it is a property of the address it came from, nothing else.
        if pkt.type == P.USER or pkt.stream in (P.STREAM_NAT, P.STREAM_NATECHO):
            self.backend.nat.handle_packet(self, pkt, endpoint)
            return

        client = self.backend.clients.by_signature(pkt.signature)
        if client is None:
            self.report_unplaced(pkt, endpoint)
            return
        client.last_seen = self.backend.now()
        link = client.link(self.port, pkt.src)

        if pkt.type == P.CONNECT:
            self.handle_connect(client, link, pkt)
        elif pkt.type == P.DATA:
            self.handle_data(client, link, pkt)
        elif pkt.type == P.PING:
            if not pkt.has(P.FLAG_ACK):
                self.send_simple(client, link, pkt)
        elif pkt.type == P.DISCONNECT:
            # Deliberately not treated as the player leaving. The game disconnects from the
            # secure server and reconnects seconds into a normal login; acting on it dropped the
            # client record mid-login and every packet after that had no owner. Who is still
            # here is worked out from when they were last heard from.
            self.log.info("%s disconnected connection %02X (kept)", client.name or endpoint[0],
                          pkt.src)
            self.send_simple(client, link, pkt)
        else:
            self.log.warning("unhandled packet type %d from %s", pkt.type, endpoint)

    def report_unplaced(self, pkt: P.Packet, endpoint) -> None:
        if endpoint in self.unplaced:
            return
        self.unplaced.add(endpoint)
        known = self.backend.clients.by_signature(pkt.signature) is None
        hint = ""
        if known and any(c.endpoint == endpoint for c in self.backend.clients):
            hint = (" - this address is known under another signature, so the game is probably"
                    " still running from before this server started; it will handshake again")
        self.log.warning("%s signed %08X, which belongs to no connection%s", endpoint,
                         pkt.signature, hint)

    def handle_syn(self, pkt: P.Packet, endpoint) -> None:
        client = self.backend.clients.for_endpoint(endpoint)
        self.unplaced.discard(endpoint)
        # A handshake on a connection that already has state means it is being established from
        # scratch - the game was restarted - and it numbers everything from the start again,
        # including the receive window our reliable packets have to fall inside. Resetting here
        # rather than on the CONNECT that follows is what makes the first fragmented reply after
        # a restart land: counting on from the old numbering is how the previous attempt lost
        # every long reply from a player's second login onward.
        existing = client.find_link(self.port, pkt.src)
        if existing is not None and (existing.signature or existing.data_seen):
            self.log.debug("%s handshaked again on %02X - starting that connection over",
                           client.name or endpoint[0], pkt.src)
            existing.restart()
        self.log.info("%s opened a connection on %02X (signature %08X)", endpoint, pkt.src,
                      client.id)
        self.send(client, None, P.Packet(pkt.dst, pkt.src, P.SYN, P.FLAG_ACK, pkt.session,
                                         pkt.signature, pkt.seq, conn_sig=client.id))

    def handle_connect(self, client, link, pkt: P.Packet) -> None:
        if pkt.has(P.FLAG_ACK):
            return
        # A CONNECT after data has flowed, or under a different session id, is a new connection -
        # the game was restarted - and it numbers everything from the start again. One before any
        # data under the same id is the same CONNECT again because our answer was slow.
        if link.data_seen or (link.signature and link.session != pkt.session):
            link.restart()
        link.signature = pkt.conn_sig
        link.session = pkt.session
        payload = b""
        if pkt.payload:
            payload = self.connect_challenge(client, pkt.src,
                                             P.unwrap_payload(pkt.stream, pkt.payload))
        reply = P.Packet(pkt.dst, pkt.src, P.CONNECT, P.FLAG_ACK, pkt.session, link.signature,
                         pkt.seq, conn_sig=client.id,
                         payload=P.wrap_payload(pkt.stream, payload) if payload else b"")
        self.send(client, link, reply)

    def connect_challenge(self, client, vport: int, plain: bytes) -> bytes:
        """The secure server's CONNECT carries the ticket and a number to answer with.

        Layout: the opaque ticket, then a buffer holding three 32-bit values encrypted under the
        session key from that ticket plus a 16-byte tail. The third value comes back incremented
        by one, which is how the client learns we could read what only the ticket's owner could.

        The key is the one issued to *this* connection when it logged in - virtual port 15 for
        the player, 14 for telemetry - which is why they are kept apart.
        """
        auth = client.auth.get(vport)
        if auth is None:
            self.log.warning("%s: a CONNECT with a ticket on %02X, but nothing logged in there",
                             client.name or client.address, vport)
            return b""
        try:
            r = Reader(plain)
            r.buffer()                                  # the ticket - the client's copy, opaque
            sealed = r.buffer()
            body = rc4(auth.session_key, sealed[:-16])
            inner = Reader(body)
            inner.u32()
            inner.u32()
            code = inner.u32()
        except Exception as e:
            self.log.warning("%s: cannot read the CONNECT challenge (%s): %s",
                             client.name or client.address, e, logs.hexs(plain))
            return b""
        self.log.debug("%s: CONNECT challenge %08X", client.name or client.address, code)
        return Writer().u32(4).u32(code + 1).bytes()

    def handle_data(self, client, link, pkt: P.Packet) -> None:
        link.session = pkt.session
        link.saw(pkt.seq)
        link.data_seen = True
        if pkt.has(P.FLAG_ACK):
            return                                       # the client acknowledging us

        reliable = pkt.has(P.FLAG_RELIABLE)
        if reliable:
            already = link.answer_to(pkt.seq)
            if already is not None:
                self.log.debug("%s sent packet %04X again - resending %d datagram(s)",
                               client.name or client.address, pkt.seq, len(already))
                if not already:
                    self.send_ack(client, link, pkt)
                for datagram in already:
                    self.sendto(datagram, client.endpoint)
                return
            link.begin_answer(pkt.seq)
        try:
            if pkt.has(P.FLAG_NEED_ACK):
                self.send_ack(client, link, pkt)
            try:
                plain = P.unwrap_payload(pkt.stream, pkt.payload)
            except P.PacketError as e:
                self.log.warning("%s: %s", client.name or client.address, e)
                return
            full = self.reassemble(client, link, pkt, plain)
            if full is None:
                return
            self.handle_rmc(client, link, pkt, full)
        finally:
            if reliable:
                link.end_answer()

    def reassemble(self, client, link, pkt: P.Packet, plain: bytes):
        """Fragments are numbered 1, 2, 3 ... with the last one carrying 0, and travel under
        consecutive sequence ids.

        They are kept by sequence id, not in order of arrival, because on a lossy network they
        do not arrive in order: the game resends only the fragment that went missing, after the
        ones behind it. Joining them as they came spliced a message out of the fragments that
        were there, threw it away as garbage, and then waited forever for the rest of a message
        that had already been discarded. A message is complete when its last fragment and every
        one in front of it, back to number 1, are here.
        """
        fragments = link.fragments
        if pkt.part == 0:
            before = fragments.get(pkt.seq - 1)
            # A message of its own: what is in front of it is the end of another message, or
            # nothing is and its size says it is all here. Otherwise it is the last fragment of
            # a message whose front may still be on its way - it can arrive first.
            if before is not None and before[0] == 0:
                return plain
            if before is None and _whole_message(plain):
                return plain
        fragments[pkt.seq] = (pkt.part, plain)
        if pkt.part == 0:
            self.log.debug("%s: packet %04X ends a message whose front is not here yet",
                           client.name or client.address, pkt.seq)
        for end in sorted(seq for seq, (part, _) in fragments.items() if part == 0):
            run = _fragment_run(fragments, end)
            if run is None:
                continue
            full = b"".join(fragments.pop(seq)[1] for seq in run)
            self.log.debug("%s: reassembled %d fragments into %d bytes",
                           client.name or client.address, len(run), len(full))
            return full
        if len(fragments) > FRAGMENTS_KEPT:
            oldest = sorted(fragments)[:len(fragments) - FRAGMENTS_KEPT]
            self.log.warning("%s: %d fragment(s) that never became a message - dropping them",
                             client.name or client.address, len(oldest))
            for seq in oldest:
                del fragments[seq]
        return None

    def handle_rmc(self, client, link, pkt, plain: bytes) -> None:
        try:
            msg = message.parse(plain)
        except Exception as e:
            self.log.warning("%s: not an RMC message (%s): %s", client.name or client.address, e,
                             logs.hexs(plain))
            return
        if msg.is_request:
            try:
                self.backend.router.dispatch(self, client, link, pkt, msg)
            except Exception:
                # The router answers every failure it knows about. This is for the ones it does
                # not, because an unanswered call is resent by the game forever.
                self.log.exception("%s: %s method %d failed on the way to an answer",
                                   client.name or client.address,
                                   self.backend.router.protocol_name(msg.protocol), msg.method)
                self.send_error(client, link, pkt, msg, Errors.CORE_UNKNOWN)
        elif msg.success:
            self.log.debug("%s answered %s call %d", client.name or client.address,
                           self.backend.router.protocol_name(msg.protocol), msg.call_id)
        else:
            self.log.info("%s refused %s call %d with 0x%08X", client.name or client.address,
                          self.backend.router.protocol_name(msg.protocol), msg.call_id, msg.error)

    # ----- sending --------------------------------------------------------------------------
    def send(self, client, link, pkt: P.Packet) -> None:
        data = pkt.encode()
        self.log.trace("-> %s %s", pkt.describe(), client.endpoint)
        self.sendto(data, client.endpoint)
        if link is not None:
            link.record(data)

    def send_simple(self, client, link, pkt: P.Packet) -> None:
        """Acknowledge a packet that needs nothing but an answer of its own kind."""
        self.send(client, link, P.Packet(pkt.dst, pkt.src, pkt.type, P.FLAG_ACK, pkt.session,
                                         self.signature(client, pkt.src), pkt.seq,
                                         conn_sig=client.id))

    def send_ack(self, client, link, pkt: P.Packet) -> None:
        self.send(client, link, P.Packet(pkt.dst, pkt.src, pkt.type, P.FLAG_ACK, pkt.session,
                                         self.signature(client, pkt.src), pkt.seq,
                                         part=pkt.part))

    def signature(self, client, vport: int) -> int:
        return client.signature_for(self.port, vport)

    def send_rmc(self, client, link, request: P.Packet, body: bytes) -> None:
        """Send one RMC message in answer to `request`, splitting it if it does not fit."""
        stream = request.stream
        signature = self.signature(client, request.src)
        if len(body) <= P.FRAGMENT_SIZE:
            self.send(client, link, P.Packet(
                request.dst, request.src, P.DATA, P.FLAG_NEED_ACK, link.session, signature,
                link.next_unreliable(request.seq), part=0,
                payload=P.wrap_payload(stream, body)))
            return
        chunks = [body[i:i + P.FRAGMENT_SIZE] for i in range(0, len(body), P.FRAGMENT_SIZE)]
        self.log.debug("%s: answer is %d bytes, sending %d fragments",
                       client.name or client.address, len(body), len(chunks))
        for n, chunk in enumerate(chunks, 1):
            last = n == len(chunks)
            self.send(client, link, P.Packet(
                request.dst, request.src, P.DATA, P.FLAG_NEED_ACK | P.FLAG_RELIABLE, link.session,
                signature, link.next_reliable(), part=0 if last else n,
                payload=P.wrap_payload(stream, chunk)))

    def send_error(self, client, link, request: P.Packet, msg, code: int) -> None:
        self.send_rmc(client, link, request, message.error(msg.protocol, msg.call_id, code))

    def send_request(self, client, link, protocol: int, method: int, params: bytes) -> int:
        """A call from us to the client - the notification protocol and the NAT traversal relay.

        There is no request to answer here, so the packet is built from the connection's own
        numbering and goes out unreliable, like a response.
        """
        call_id = client.next_call_id()
        body = message.request(protocol, call_id, method, params)
        vport = link.vport
        pkt = P.Packet(self.backend.server_vport(vport), vport, P.DATA, P.FLAG_NEED_ACK,
                       link.session, self.signature(client, vport), link.next_unreliable(),
                       part=0, payload=P.wrap_payload(P.stream_of(vport), body))
        self.send(client, link, pkt)
        return call_id
