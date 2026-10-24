"""The NAT echo: telling a game what address and port its own peer-to-peer socket is seen from.

The game asks by sending the secure port, from its P2P socket (UDP 2346), a PRUDP packet of type
5 on the NAT echo stream whose whole payload is two bytes: the NAT transport's header, 0xA5, and
a message type. Type 2 is the question - it comes four times a second from the job that wants the
public URL, ten tries then a restart, for as long as the game runs. Type 3 is a keep-alive every
thirty seconds and is answered by nobody.

The answer must be a bare datagram, not a PRUDP packet: the game's socket hands a datagram to
its NAT code only when the first byte is that header, strips it, and expects the type followed by
a station URL as a C string. Everything the previous attempt tried - the probe mirrored back, the
type with its top bit set, the address in various byte orders, a sweep of nineteen shapes - was
wrapped in a PRUDP header, began with 0x71 instead of 0xA5, and never got past the socket.
"""

from . import log as logs

HEADER = 0xA5
ASK_PUBLIC_URL = 0x02
KEEP_ALIVE = 0x03


class NatEcho:
    def __init__(self, backend):
        self.backend = backend
        self.log = logs.get("nat")
        self.answered = 0
        self.keep_alives = 0
        self.enabled = True

    def looks_like_probe(self, data: bytes) -> bool:
        """A probe that arrives without a PRUDP header around it.

        This game wraps them, so this is a safety net rather than the normal path: a two-byte
        datagram is not valid PRUDP and would only produce a parse warning four times a second.
        """
        return len(data) in (2, 3) and data[0] == HEADER

    def handle(self, server, data: bytes, endpoint) -> None:
        self.respond(server, data, endpoint)

    def handle_packet(self, server, pkt, endpoint) -> None:
        """A probe inside a PRUDP packet, which is how this game sends them."""
        if not pkt.payload:
            self.log.debug("%s sent an empty NAT packet on %02X", endpoint, pkt.src)
            return
        self.respond(server, pkt.payload, endpoint)

    def respond(self, server, probe: bytes, endpoint) -> None:
        answer, why = self.answer(probe, endpoint)
        if answer is None:
            if why == "keep-alive":
                self.keep_alives += 1
                if self.keep_alives % 20 == 1:
                    self.log.debug("keep-alive from %s (%d so far)", endpoint, self.keep_alives)
            else:
                self.log.info("probe from %s not answered: %s (%s)", endpoint, why,
                              logs.hexs(probe))
            return
        server.sendto(answer, endpoint)
        self.answered += 1
        # Four a second while the job runs, so only the first few are worth a line each.
        if self.answered <= 3 or self.answered % 50 == 0:
            self.log.info("told %s its public address: %s (%d answered)", endpoint, why,
                          self.answered)
        else:
            self.log.debug("answered %s: %s", endpoint, why)

    def answer(self, probe: bytes, endpoint):
        """The datagram to send back, ready for the wire, and a note for the log."""
        if not self.enabled:
            return None, "answering is switched off"
        if not probe or len(probe) < 2:
            return None, "no header and type to go by"
        kind = probe[-1]
        if kind == KEEP_ALIVE:
            return None, "keep-alive"
        if kind != ASK_PUBLIC_URL:
            return None, "unknown type %02X" % kind
        url = "udp:/address=%s;port=%d" % (endpoint[0], endpoint[1])
        # Whatever precedes the type is the transport header - one byte from this game, but
        # nothing about the exchange depends on that.
        return probe[:-1] + bytes([ASK_PUBLIC_URL]) + url.encode("ascii") + b"\x00", url
