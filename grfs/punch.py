"""The Storm NAT-type detector's server side, so that every game here is told its NAT is Open.

The game has exactly one source for the NAT type its screens show and its scripts test: the Storm
"punch" client, started once at launch from the configuration key punch_DetectUrls. With no key -
which is what Ubisoft's own configuration service still answers - the type stays Unknown, and
"Unable to join due to STRICT NAT" and the squad room's "OPEN or MODERATE" warning are decided by
script from that value. Unknown passes a gate written as "not Strict" and fails one written as
"Open or Moderate", and which one the scripts use cannot be read out of the executable. Open
passes both.

The client sends 13-byte requests and accepts 19-byte replies (932490, 932190):

    request  00 0D | 00 cmd | seq | 00 | 7 bytes of whatever was on its stack
    reply    00 13 | 00 type | 9 bytes, here the request's own | IP, byte-reversed | port, BE

cmd 1 asks "what address do you see me from", 2 "answer from another port", 3 "answer from
another server". The reply's type is what moves the client's state machine, and the path to Open
takes four round trips:

    1. cmd 1 -> P      type 1, the mapped address     Filter1 -> Filter2, sends cmd 3
    2. cmd 3 -> P      type 3                          Filter2 -> Filter3, filtering = 1
    3. cmd 2 -> P      type 2                          Filter3 -> Map1, sends cmd 1 to P+1
    4. cmd 1 -> P+1    type 1, the SAME mapped port    mapping = 1: Open

Three things in the client decide how this has to be written:

* Every datagram of 13 bytes or more cancels its packet timer before the type is even looked at,
  and most states never resend. So exactly one reply per request, from the socket that got it -
  a second one answers a question that was never asked and turns Open into Moderate.
* Step 4 is compared with step 1's port. A symmetric NAT gives the second socket a new port, and
  reflecting that sends the client to its Strict branch. The port a game was last seen from on
  P is remembered for a minute, and P+1 answers with that one instead.
* The source address of a reply is never checked, so "from another port" and "from another
  server" are answered from the same socket. Only the reply type matters.

The second value in punch_DetectUrls is only ever contacted on the paths a game takes when
something is lost (a retry after three timeouts, the Strict branch), so it names P+1 too.
"""

import asyncio
import random
import socket
import struct
import time

from . import log as logs
from .net import ignore_port_unreachable

REQUEST_LENGTH = 13
REPLY_LENGTH = 19

BINDING = 1                  # what address do you see me from?
FROM_ANOTHER_PORT = 2
FROM_ANOTHER_SERVER = 3

REPLY = 1
REPLY_NEXT_PORT = 2
REPLY_NEXT_SERVER = 3
ANSWERS = {BINDING: REPLY, FROM_ANOTHER_PORT: REPLY_NEXT_PORT,
           FROM_ANOTHER_SERVER: REPLY_NEXT_SERVER}

# How long a game's port on the first socket is remembered for its last step. The whole exchange
# takes four round trips; the client gives up on any one after 2.5 seconds and on all of it
# after 20.
REMEMBER = 60.0

# Where the test harness looks for two free ports in a row. Not "any port the system offers":
# Windows hands those out one after another, so the port above the one just given is the next
# one anybody gets, and with a few servers starting at once the pair was taken more often than
# not. Below the system's own range nothing hands ports out on its own.
ANY_PORTS = (20000, 45000)
ANY_PORT_ATTEMPTS = 50


def reply(request: bytes, kind: int, ip: str, port: int) -> bytes:
    """The 19 bytes the client accepts: its own request bytes 4..12 in the middle, the address
    byte-reversed (the client swaps the dword and stores it in network order) and the port
    big-endian."""
    return (struct.pack(">HH", REPLY_LENGTH, kind) + bytes(request[4:REQUEST_LENGTH])
            + socket.inet_aton(ip)[::-1] + struct.pack(">H", port))


def command_of(data: bytes):
    """The command in a detect request, or None for anything else."""
    if len(data) < REQUEST_LENGTH:
        return None
    length, cmd = struct.unpack_from(">HH", data)
    if length != REQUEST_LENGTH or cmd not in ANSWERS:
        return None
    return cmd


class _Socket(asyncio.DatagramProtocol):
    def __init__(self, responder, second: bool):
        self.responder = responder
        self.second = second
        self.transport = None

    def connection_made(self, transport):
        self.transport = transport
        # A reply to a game that has gone would otherwise stop this socket reading for good
        # (see grfs.net).
        if not ignore_port_unreachable(transport):
            self.responder.log.warning("could not switch off port-unreachable errors on a NAT"
                                       " detector socket")

    def datagram_received(self, data, endpoint):
        self.responder.handle(self, data, endpoint)

    def error_received(self, exc):
        self.responder.log.warning("%s", exc)


class PunchResponder:
    """Two UDP sockets on consecutive ports of the same address, answering detect requests."""

    def __init__(self, backend):
        self.backend = backend
        self.log = logs.get("punch")
        self.port = 0
        self.received = 0
        self.sent = 0
        self.ignored = 0
        self.resolved = 0
        self.sockets = []
        # ip -> {port on the first socket: when}. Swept whole every REMEMBER seconds: an
        # address that asks once and never again would otherwise stay for good, and anything
        # on the internet can send thirteen bytes from as many addresses as it likes.
        self._seen = {}
        self._swept = time.monotonic()

    async def start(self, port: int, bind: str = None, any_port: bool = False) -> None:
        """Bind `port` and the one above it, or - with `any_port`, for the test harness - a free
        pair picked at random from ANY_PORTS."""
        loop = asyncio.get_running_loop()
        bind = bind or self.backend.config.get("bind", "0.0.0.0")
        attempts = ANY_PORT_ATTEMPTS if any_port else 1
        for attempt in range(attempts):
            want = random.randrange(*ANY_PORTS) if any_port else port
            first = None
            try:
                first, _ = await loop.create_datagram_endpoint(
                    lambda: _Socket(self, False), local_addr=(bind, want))
                second, _ = await loop.create_datagram_endpoint(
                    lambda: _Socket(self, True), local_addr=(bind, want + 1))
            except OSError:
                if first is not None:
                    first.close()
                if attempt == attempts - 1:
                    raise
                continue
            self.sockets = [first, second]
            self.port = first.get_extra_info("sockname")[1]
            break
        self.log.info("NAT type detection on %s:%d and %d (UDP)", bind, self.port, self.port + 1)

    def close(self) -> None:
        for transport in self.sockets:
            transport.close()
        self.sockets = []

    # -----------------------------------------------------------------------------------------
    def handle(self, sock: _Socket, data: bytes, endpoint) -> None:
        self.received += 1
        answer, note = self.answer(data, endpoint, sock.second)
        if answer is None:
            self.ignored += 1
            if self.ignored <= 3 or self.ignored % 100 == 0:
                self.log.info("%s:%d sent %d bytes that are not a detect request: %s",
                              endpoint[0], endpoint[1], len(data), logs.hexs(data[:16]))
            return
        sock.transport.sendto(answer, endpoint)
        self.sent += 1
        if sock.second and command_of(data) == BINDING:
            # The last step of the way to Open; the client has nothing more to ask.
            self.resolved += 1
            self.log.info("%s:%d %s - its NAT type is Open (%d told so far)", endpoint[0],
                          endpoint[1], note, self.resolved)
        else:
            self.log.debug("%s:%d %s", endpoint[0], endpoint[1], note)

    def answer(self, data: bytes, endpoint, second: bool = False):
        """The reply to send and a note for the log, or (None, why)."""
        cmd = command_of(data)
        if cmd is None:
            return None, "not a detect request"
        ip, port = endpoint[0], endpoint[1]
        mapped = port
        now = time.monotonic()
        if now - self._swept > REMEMBER:
            self._swept = now
            for other in list(self._seen):
                self._forget(other, now)
        self._forget(ip, now)
        seen = self._seen.setdefault(ip, {})
        if not second:
            if cmd == BINDING:
                seen[port] = now
            note = "cmd %d, seen from port %d" % (cmd, port)
        elif cmd != BINDING:
            note = "cmd %d on the second port" % cmd
        elif port in seen or not seen:
            note = "seen from port %d again" % port
        else:
            mapped = max(seen, key=seen.get)
            note = ("seen from port %d, not %d as before (a symmetric NAT) - told %d"
                    % (port, mapped, mapped))
        if not seen:
            self._seen.pop(ip, None)
        return reply(data, ANSWERS[cmd], ip, mapped), note

    def _forget(self, ip: str, now: float) -> None:
        """Drop the ports `ip` was seen from longer ago than REMEMBER, and the address with
        them once none is left."""
        seen = self._seen.get(ip)
        if seen is None:
            return
        for old in [p for p, when in seen.items() if now - when > REMEMBER]:
            del seen[old]
        if not seen:
            del self._seen[ip]
