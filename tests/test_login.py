"""The whole login, driven through the real server objects with a stand-in for the socket.

What a game does, in order: handshake with the authentication server, log in, ask for a ticket,
handshake with the secure server (proving it can read the ticket), register its addresses. Every
step here is checked the way the client checks it - the ticket is really decrypted with a key
derived from the pid, the challenge is really answered with the number plus one - so a change
that breaks the login fails here instead of in front of the game.
"""

import os
import unittest

from grfs.backend import Backend
from grfs.config import Config
from grfs.kerberos import SERVER_PID, TRACKING_NAME, TRACKING_PID
from grfs.net import PrudpServer
from grfs.prudp import packet as P
from grfs.prudp.crypto import derive_key, hmac_md5, rc4
from grfs.protocols import SECURE_CONNECTION, TICKET_GRANTING
from grfs.rmc import message
from grfs.rmc.stream import Reader, Writer

PLAYER_VPORT = 0x3F        # stream 3 (RVSec), port 15 - the player's own connection
TELEMETRY_VPORT = 0x3E     # port 14 - the telemetry connection beside it
SERVER_VPORT = 0x31        # stream 3, port 1 - our end


class FakeTransport:
    def __init__(self):
        self.sent = []

    def sendto(self, data, endpoint):
        self.sent.append((data, endpoint))


class GameSide:
    """The client half of one connection, built the way the game builds it."""

    def __init__(self, server, endpoint, vport=PLAYER_VPORT, signature=0x7F260001):
        self.server = server
        self.endpoint = endpoint
        self.vport = vport
        self.signature = signature      # what we tell the server to sign its replies with
        self.server_signature = 0       # what the server told us to sign with
        self.session = 0x52
        self.seq = 0
        self.call_id = 0

    # ----- plumbing -------------------------------------------------------------------------
    def feed(self, pkt) -> list:
        self.server.transport.sent.clear()
        self.server.datagram_received(pkt.encode(), self.endpoint)
        out = []
        for data, _ in self.server.transport.sent:
            out.extend(P.parse(data))
        return out

    def handshake(self) -> None:
        syn = P.Packet(self.vport, SERVER_VPORT, P.SYN, P.FLAG_NEED_ACK, 0, 0, 0)
        reply, = self.feed(syn)
        assert reply.type == P.SYN and reply.has(P.FLAG_ACK), reply.describe()
        self.server_signature = reply.conn_sig
        self.seq = 1
        connect = P.Packet(self.vport, SERVER_VPORT, P.CONNECT,
                           P.FLAG_RELIABLE | P.FLAG_NEED_ACK, self.session,
                           self.server_signature, self.seq, conn_sig=self.signature)
        reply, = self.feed(connect)
        assert reply.type == P.CONNECT and reply.signature == self.signature, reply.describe()

    def connect_with_ticket(self, ticket: bytes, session_key: bytes, code: int) -> int:
        """The secure server's CONNECT: the ticket, and a number only its owner can read."""
        sealed = rc4(session_key, Writer().u32(0).u32(0).u32(code).bytes())
        payload = (Writer().buffer(ticket)
                   .buffer(sealed + hmac_md5(session_key, sealed)).bytes())
        self.seq = 1
        connect = P.Packet(self.vport, SERVER_VPORT, P.CONNECT,
                           P.FLAG_RELIABLE | P.FLAG_NEED_ACK, self.session,
                           self.server_signature, self.seq, conn_sig=self.signature,
                           payload=P.wrap_payload(P.STREAM_RVSEC, payload))
        reply, = self.feed(connect)
        assert reply.type == P.CONNECT, reply.describe()
        plain = P.unwrap_payload(P.STREAM_RVSEC, reply.payload)
        r = Reader(plain)
        assert r.u32() == 4
        return r.u32()

    def call(self, protocol: int, method: int, params: bytes, reliable: bool = True):
        """One RMC request; returns (acks, response message)."""
        self.seq += 1
        self.call_id += 1
        body = message.request(protocol, self.call_id, method, params)
        flags = P.FLAG_NEED_ACK | (P.FLAG_RELIABLE if reliable else 0)
        pkt = P.Packet(self.vport, SERVER_VPORT, P.DATA, flags, self.session,
                       self.server_signature, self.seq, part=0,
                       payload=P.wrap_payload(P.STREAM_RVSEC, body))
        replies = self.feed(pkt)
        acks = [p for p in replies if p.has(P.FLAG_ACK)]
        data = [p for p in replies if not p.has(P.FLAG_ACK)]
        assert data, "no answer to %s/%s: %s" % (protocol, method, [p.describe() for p in replies])
        joined = b"".join(P.unwrap_payload(P.STREAM_RVSEC, p.payload) for p in data)
        return acks, message.parse(joined), data

    def repeat_last(self, protocol: int, method: int, params: bytes):
        """Send the previous request again under the same sequence id, as the game does when our
        acknowledgement is slow."""
        body = message.request(protocol, self.call_id, method, params)
        pkt = P.Packet(self.vport, SERVER_VPORT, P.DATA, P.FLAG_NEED_ACK | P.FLAG_RELIABLE,
                       self.session, self.server_signature, self.seq, part=0,
                       payload=P.wrap_payload(P.STREAM_RVSEC, body))
        return self.feed(pkt)


def read_ticket(blob: bytes, pid: int, password: bytes):
    """Undo what the server did, exactly as the client does."""
    key = derive_key(pid, password)
    body, mac = blob[:-16], blob[-16:]
    assert hmac_md5(key, body) == mac, "the ticket's HMAC does not check out"
    r = Reader(rc4(key, body))
    session_key = r.take(16)
    server_pid = r.u32()
    inner = r.buffer()
    return session_key, server_pid, inner


def read_login_reply(body: bytes) -> dict:
    r = Reader(body)
    out = {"retval": r.u32(), "pid": r.u32(), "ticket": r.buffer(), "url": r.string(),
           "special": [r.u8() for _ in range(r.u32())], "special_url": r.string(),
           "message": r.string()}
    out["tail"] = r.rest()
    return out


class Login(unittest.TestCase):
    def setUp(self):
        config = Config(path=os.devnull, overrides={"bind": "127.0.0.1", "advertise": ""})
        self.backend = Backend(config=config, data_dir="")
        self.auth = PrudpServer(self.backend, "auth", self.backend.auth_port)
        self.auth.transport = FakeTransport()
        self.secure = PrudpServer(self.backend, "secure", self.backend.secure_port)
        self.secure.transport = FakeTransport()
        self.backend.servers = {"auth": self.auth, "secure": self.secure}
        self.endpoint = ("127.0.0.1", 2347)

    def login(self, name="tester", vport=PLAYER_VPORT):
        game = GameSide(self.auth, self.endpoint, vport)
        game.handshake()
        schema = self.backend.schema
        method = schema.method("TicketGrantingProtocol", 2)
        params = schema.encode_request(method, {
            "strUserName": name,
            "oExtraData": {"__class__": "UbiAuthenticationLoginCustomData",
                           "m_userName": name, "m_onlineKey": "KEY-SHOULD-NOT-BE-READ",
                           "m_password": "PASSWORD-SHOULD-NOT-BE-READ"}})
        acks, reply, _ = game.call(TICKET_GRANTING, 2, params)
        self.assertEqual(len(acks), 1, "the request should be acknowledged once")
        self.assertTrue(reply.success)
        return game, read_login_reply(reply.body)

    # ----- the steps -------------------------------------------------------------------------
    def test_login_hands_out_a_ticket_the_client_can_read(self):
        game, out = self.login()
        self.assertEqual(out["retval"], 0x00010001)
        self.assertEqual(out["pid"], 0x1234)            # first player on a fresh register
        session_key, server_pid, inner = read_ticket(out["ticket"], out["pid"], b"UbiDummyPwd")
        self.assertEqual(server_pid, SERVER_PID)
        self.assertEqual(len(session_key), 16)
        self.assertEqual(len(inner), 36)
        self.assertEqual(out["url"],
                         "prudps:/address=127.0.0.1;port=21031;CID=1;PID=4096;sid=1;stream=3;"
                         "type=2")
        self.assertEqual(out["special"], [])
        self.assertEqual(out["tail"], b"\x01\x00\x00\x00")

    def test_credentials_never_leave_the_wire(self):
        game, out = self.login()
        client = self.backend.clients.by_signature(game.server_signature)
        state = repr(client.__dict__) + repr(self.backend.accounts.get("tester").as_dict())
        self.assertNotIn("PASSWORD-SHOULD-NOT-BE-READ", state)
        self.assertNotIn("KEY-SHOULD-NOT-BE-READ", state)

    def test_request_ticket_returns_the_same_ticket(self):
        game, out = self.login()
        schema = self.backend.schema
        method = schema.method("TicketGrantingProtocol", 3)
        params = schema.encode_request(method, {"idSource": out["pid"], "idTarget": SERVER_PID})
        _, reply, _ = game.call(TICKET_GRANTING, 3, params)
        r = Reader(reply.body)
        self.assertEqual(r.u32(), 0x00010001)
        self.assertEqual(r.buffer(), out["ticket"])

    def test_telemetry_login_does_not_take_over_the_player(self):
        game, out = self.login()
        client = self.backend.clients.by_signature(game.server_signature)
        telemetry = GameSide(self.auth, self.endpoint, TELEMETRY_VPORT, signature=0x7F260002)
        telemetry.handshake()
        method = self.backend.schema.method("TicketGrantingProtocol", 1)
        params = self.backend.schema.encode_request(method, {"strUserName": TRACKING_NAME})
        _, reply, _ = telemetry.call(TICKET_GRANTING, 1, params)
        tracking = read_login_reply(reply.body)
        self.assertEqual(tracking["pid"], TRACKING_PID)
        self.assertEqual(tracking["tail"], b"")          # plain Login carries no tail
        # The player keeps their own identity, and each connection its own key.
        self.assertEqual((client.name, client.pid), ("tester", 0x1234))
        self.assertNotEqual(client.auth[PLAYER_VPORT].session_key,
                            client.auth[TELEMETRY_VPORT].session_key)
        read_ticket(tracking["ticket"], TRACKING_PID, b"JaDe!")

    def test_secure_connect_answers_the_challenge_and_registers(self):
        game, out = self.login()
        session_key, _, _ = read_ticket(out["ticket"], out["pid"], b"UbiDummyPwd")

        secure = GameSide(self.secure, self.endpoint, PLAYER_VPORT, signature=0x7F270001)
        secure.handshake()
        self.assertEqual(secure.connect_with_ticket(out["ticket"], session_key, 0xE2450886),
                         0xE2450887)

        schema = self.backend.schema
        method = schema.method("SecureConnectionProtocol", 4)
        urls = ["prudp:/address=127.0.0.1;port=2347;sid=15",
                "prudp:/address=192.168.1.8;port=2347;sid=15"]
        params = schema.encode_request(method, {
            "vecMyURLs": urls,
            "hCustomData": {"__class__": "UbiAuthenticationLoginCustomData",
                            "m_userName": "tester", "m_onlineKey": "", "m_password": ""}})
        _, reply, _ = secure.call(SECURE_CONNECTION, 4, params)
        r = Reader(reply.body)
        self.assertEqual(r.u32(), 0x00010001)
        self.assertEqual(r.u32(), 78)                    # first connection id
        self.assertEqual(r.string(),
                         "prudps:/address=127.0.0.1;port=2347;CID=1;PID=4660;sid=1;RVCID=78;"
                         "stream=3;type=2")
        client = self.backend.clients.by_signature(secure.server_signature)
        self.assertEqual(client.urls, urls)

    def test_a_resent_request_is_answered_with_the_same_bytes(self):
        game, out = self.login()
        schema = self.backend.schema
        method = schema.method("TicketGrantingProtocol", 3)
        params = schema.encode_request(method, {"idSource": out["pid"], "idTarget": SERVER_PID})
        _, _, first = game.call(TICKET_GRANTING, 3, params)
        before = [p.encode() for p in first]
        again = game.repeat_last(TICKET_GRANTING, 3, params)
        resent = [p.encode() for p in again if not p.has(P.FLAG_ACK)]
        self.assertEqual(resent, before, "the copy should be answered with the original bytes")
        # And it must not have been handled a second time.
        self.assertEqual(self.backend.accounts.get("tester").pid, 0x1234)

    def test_unknown_method_gets_an_error_not_silence(self):
        game, _ = self.login()
        _, reply, _ = game.call(TICKET_GRANTING, 5, b"\x00" * 4)
        self.assertFalse(reply.success)
        self.assertTrue(reply.error & 0x80000000)


if __name__ == "__main__":
    unittest.main()
