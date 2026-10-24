"""A stand-in for the game: logs in over real sockets and says what happened.

The thing the previous attempt never had. Every check cost a launch of the game, a look at a
menu, and a guess about which of the two machines was at fault; a protocol mistake took an
evening to find. This walks the same path the game walks - handshake, log in, ask for a ticket,
prove it on the secure server, register, send a NAT probe - and prints each step, so a change can
be checked in a second and from one machine.

    python tools/fake_client.py                      # against a server on this machine
    python tools/fake_client.py --host 192.168.1.8   # against the other PC
    python tools/fake_client.py --config             # ask the configuration service first

It is not a game and never will be: no session, no peer traffic. It answers "did I break the
login", nothing more.
"""

import argparse
import json
import os
import socket
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from grfs.prudp import packet as P                                        # noqa: E402
from grfs.prudp.crypto import derive_key, hmac_md5, rc4                   # noqa: E402
from grfs.protocols import SECURE_CONNECTION, TICKET_GRANTING             # noqa: E402
from grfs.rmc import message                                             # noqa: E402
from grfs.rmc.ddl import Schema                                          # noqa: E402
from grfs.rmc.stream import Reader, Writer                               # noqa: E402

SCHEMA = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "schema",
                      "grfs-ddl.json")
PLAYER_VPORT = 0x3F
SERVER_VPORT = 0x31

GREEN, RED, DIM, OFF = "\033[32m", "\033[31m", "\033[90m", "\033[0m"


def ok(text):
    print(" %s+%s %s" % (GREEN, OFF, text))


def bad(text):
    print(" %s!%s %s" % (RED, OFF, text))


def note(text):
    print("   %s%s%s" % (DIM, text, OFF))


class Connection:
    """One PRUDP connection to one of the servers.

    Both servers are reached from the *same* socket, because that is what the game does - one
    socket, several connections told apart by their virtual port - and the server identifies a
    game by the address its packets come from. Using a socket each looks like two different
    players and the secure server then has no ticket to check.
    """

    def __init__(self, host, port, signature, timeout=3.0, vport=PLAYER_VPORT, sock=None):
        self.endpoint = (host, port)
        self.vport = vport
        self.signature = signature
        self.server_signature = 0
        self.session = 0x52
        self.seq = 0
        self.call_id = 0
        self.owns_socket = sock is None
        self.sock = sock or socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.settimeout(timeout)

    def close(self):
        if self.owns_socket:
            self.sock.close()

    def exchange(self, pkt, expect=1):
        self.sock.sendto(pkt.encode(), self.endpoint)
        out = []
        while len(out) < expect:
            try:
                data, _ = self.sock.recvfrom(65535)
            except socket.timeout:
                break
            out.extend(P.parse(data))
        return out

    def handshake(self):
        replies = self.exchange(P.Packet(self.vport, SERVER_VPORT, P.SYN, P.FLAG_NEED_ACK,
                                         0, 0, 0))
        if not replies:
            raise TimeoutError("no answer to SYN from %s:%d" % self.endpoint)
        self.server_signature = replies[0].conn_sig
        self.seq = 1
        connect = P.Packet(self.vport, SERVER_VPORT, P.CONNECT,
                           P.FLAG_RELIABLE | P.FLAG_NEED_ACK, self.session,
                           self.server_signature, self.seq, conn_sig=self.signature)
        replies = self.exchange(connect)
        if not replies:
            raise TimeoutError("no answer to CONNECT")
        return self.server_signature

    def connect_with_ticket(self, ticket, session_key, code=0xE2450886):
        sealed = rc4(session_key, Writer().u32(0).u32(0).u32(code).bytes())
        payload = Writer().buffer(ticket).buffer(sealed + hmac_md5(session_key, sealed)).bytes()
        self.seq = 1
        replies = self.exchange(P.Packet(
            self.vport, SERVER_VPORT, P.CONNECT, P.FLAG_RELIABLE | P.FLAG_NEED_ACK, self.session,
            self.server_signature, self.seq, conn_sig=self.signature,
            payload=P.wrap_payload(P.STREAM_RVSEC, payload)))
        if not replies or not replies[0].payload:
            raise ValueError("the secure server did not answer the challenge")
        r = Reader(P.unwrap_payload(P.STREAM_RVSEC, replies[0].payload))
        r.u32()
        return r.u32(), code + 1

    def call(self, protocol, method, params):
        self.seq += 1
        self.call_id += 1
        body = message.request(protocol, self.call_id, method, params)
        pkt = P.Packet(self.vport, SERVER_VPORT, P.DATA,
                       P.FLAG_RELIABLE | P.FLAG_NEED_ACK, self.session, self.server_signature,
                       self.seq, part=0, payload=P.wrap_payload(P.STREAM_RVSEC, body))
        # An acknowledgement and then the answer, which may itself arrive in fragments.
        replies = self.exchange(pkt, expect=2)
        data = [p for p in replies if not p.has(P.FLAG_ACK)]
        while data and data[-1].part != 0:
            try:
                more, _ = self.sock.recvfrom(65535)
            except socket.timeout:
                break
            data.extend(p for p in P.parse(more) if not p.has(P.FLAG_ACK))
        if not data:
            raise TimeoutError("no answer to protocol 0x%X method %d" % (protocol, method))
        joined = b"".join(P.unwrap_payload(P.STREAM_RVSEC, p.payload) for p in data)
        return message.parse(joined)


def read_ticket(blob, pid, password):
    key = derive_key(pid, password)
    body, mac = blob[:-16], blob[-16:]
    if hmac_md5(key, body) != mac:
        raise ValueError("the ticket's HMAC does not check out - wrong password or pid")
    r = Reader(rc4(key, body))
    return r.take(16), r.u32(), r.buffer()


def fetch_config(host, port):
    url = ("http://%s:%d/OnlineConfigService.svc/GetOnlineConfig?onlineConfigID="
           "23ad683803a0457cabce83f905811dbc&target=client" % (host, port))
    request = urllib.request.Request(url, headers={"Host": "onlineconfigservice.ubi.com",
                                                   "Accept": "*/*"})
    with urllib.request.urlopen(request, timeout=5) as response:
        rows = json.loads(response.read().decode("utf-8"))
    return {row["Name"]: row["Values"][0] for row in rows}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--auth-port", type=int, default=21030)
    parser.add_argument("--secure-port", type=int, default=21031)
    parser.add_argument("--config-port", type=int, default=80)
    parser.add_argument("--name", default="fakeclient")
    parser.add_argument("--config", action="store_true",
                        help="ask the configuration service where the server is, as the game does")
    parser.add_argument("--nat", action="store_true",
                        help="also send a NAT echo probe from this socket")
    args = parser.parse_args(argv)

    schema = Schema().load_ddl_json(SCHEMA)
    auth_port, secure_port = args.auth_port, args.secure_port
    failures = 0

    print("\n%sGRFS emulator check%s - %s" % ("\033[1m", OFF, args.host))

    if args.config:
        print("\nconfiguration service")
        try:
            values = fetch_config(args.host, args.config_port)
            url = values.get("SandboxUrl", "")
            ok("SandboxUrl = %s" % url)
            if "address=" in url and "port=" in url:
                host = url.split("address=")[1].split(";")[0]
                auth_port = int(url.split("port=")[1].split(";")[0])
                secure_port = auth_port + 1
                if host != args.host:
                    note("it points at %s; using that for the ports only" % host)
                note("so the authentication server is on %d and the secure one on %d"
                     % (auth_port, secure_port))
        except Exception as e:
            bad("no configuration: %s" % e)
            failures += 1

    print("\nauthentication server :%d" % auth_port)
    auth = Connection(args.host, auth_port, 0x7F260001)
    ticket = session_key = None
    pid = 0
    try:
        signature = auth.handshake()
        ok("handshake done, it signs as %08X" % signature)

        method = schema.method("TicketGrantingProtocol", 2)
        params = schema.encode_request(method, {
            "strUserName": args.name,
            "oExtraData": {"__class__": "UbiAuthenticationLoginCustomData",
                           "m_userName": args.name, "m_onlineKey": "", "m_password": ""}})
        reply = auth.call(TICKET_GRANTING, 2, params)
        if not reply.success:
            raise ValueError("login refused with 0x%08X" % reply.error)
        r = Reader(reply.body)
        result, pid, ticket = r.u32(), r.u32(), r.buffer()
        secure_url = r.string()
        ok("logged in as %s, pid 0x%X, result 0x%08X" % (args.name, pid, result))
        ok("secure server at %s" % secure_url)
        session_key, server_pid, inner = read_ticket(ticket, pid, b"UbiDummyPwd")
        ok("the ticket decrypts: session key %s..., server pid 0x%X, %d opaque bytes"
           % (session_key[:4].hex(), server_pid, len(inner)))
        if "address=" in secure_url:
            told = secure_url.split("address=")[1].split(";")[0]
            if told in ("0.0.0.0", "", "255.255.255.255"):
                bad("that address is not one anybody can connect to")
                failures += 1

        method = schema.method("TicketGrantingProtocol", 3)
        reply = auth.call(TICKET_GRANTING, 3,
                          schema.encode_request(method, {"idSource": pid,
                                                         "idTarget": server_pid}))
        r = Reader(reply.body)
        r.u32()
        again = r.buffer()
        ok("the ticket came back %s" % ("identical" if again == ticket else "DIFFERENT"))
        if again != ticket:
            failures += 1
    except Exception as e:
        bad(str(e))
        failures += 1

    if ticket and session_key:
        print("\nsecure server :%d" % secure_port)
        # The same socket, so the server sees one game rather than two.
        secure = Connection(args.host, secure_port, 0x7F270001, sock=auth.sock)
        try:
            secure.handshake()
            ok("handshake done")
            got, expected = secure.connect_with_ticket(ticket, session_key)
            if got == expected:
                ok("it answered the challenge: %08X" % got)
            else:
                bad("it answered %08X, expected %08X" % (got, expected))
                failures += 1

            method = schema.method("SecureConnectionProtocol", 4)
            urls = ["prudp:/address=%s;port=2347;sid=15" % local_address(args.host)]
            reply = secure.call(SECURE_CONNECTION, 4, schema.encode_request(method, {
                "vecMyURLs": urls,
                "hCustomData": {"__class__": "UbiAuthenticationLoginCustomData",
                                "m_userName": args.name, "m_onlineKey": "", "m_password": ""}}))
            if not reply.success:
                raise ValueError("registration refused with 0x%08X" % reply.error)
            r = Reader(reply.body)
            r.u32()
            ok("registered as connection %d" % r.u32())
            ok("it calls us %s" % r.string())
        except Exception as e:
            bad(str(e))
            failures += 1
        finally:
            secure.close()
    auth.close()

    if args.nat:
        print("\nNAT echo :%d" % secure_port)
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.settimeout(3)
        try:
            # What the game's peer-to-peer socket sends: a type 5 packet whose whole payload is
            # the NAT header and the message type.
            pkt = P.Packet(0x71, 0x71, P.USER, 0, 0, 0, 0, payload=b"\xA5\x02")
            probe.sendto(pkt.encode(), (args.host, secure_port))
            data, _ = probe.recvfrom(65535)
            if data[0] == 0xA5 and data[1] == 0x02:
                ok("it told us our address: %s" % data[2:].rstrip(b"\x00").decode("latin-1"))
            else:
                bad("the answer does not start with A5 02: %s" % data[:16].hex(" "))
                failures += 1
        except socket.timeout:
            bad("no answer to the probe")
            failures += 1
        finally:
            probe.close()

    print("\n%s%s%s\n" % (RED if failures else GREEN,
                          "%d step(s) failed" % failures if failures else "everything answered",
                          OFF))
    return 1 if failures else 0


def local_address(host):
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect((host, 9))
        return probe.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        probe.close()


if __name__ == "__main__":
    sys.exit(main())
