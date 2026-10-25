"""The two tools in tools/, run against a real server, so that they cannot quietly rot."""

import contextlib
import importlib.util
import io
import os
import time
import unittest

from tests.live import LiveTest

TOOLS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools")


def load(name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(TOOLS, name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeClient(LiveTest):
    def test_every_check_passes(self):
        tool = load("fake_client")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = tool.main(["--auth-port", str(self.server.ports["auth"]),
                              "--secure-port", "1",          # --config must correct this
                              "--config", "--config-port", str(self.server.ports["onlineconfig"]),
                              "--nat", "--friends"])
        self.assertEqual(code, 0, out.getvalue())
        self.assertIn("everything answered", out.getvalue())
        self.assertIn("the Uplay tab resolves: fake-b -> fake-b", out.getvalue())

    def test_a_server_that_is_not_there_is_a_failure(self):
        tool = load("fake_client")
        self.server.stop()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = tool.main(["--auth-port", str(self.server.ports["auth"])])
        self.assertEqual(code, 1)
        self.assertIn("no answer to SYN", out.getvalue())


class FakeGame(LiveTest):
    def test_a_session_typed_by_hand(self):
        tool = load("fake_game")
        alice = self.player("alice")
        bob = self.player("bob")
        alice.call("FriendsProtocol", "AddFriendByName", {"strPlayerName": "bob",
                                                          "strMessage": "co-op?"})
        # The request's notification is on its way to bob; the first command shows it.
        deadline = time.monotonic() + 5
        while bob.secure.requests.empty() and time.monotonic() < deadline:
            time.sleep(0.02)
        game = tool.FakeGame(bob)
        out = io.StringIO()
        game.stdout = out
        script = ["whoami", "invites", "accept alice", "list", "uplay", "search ali",
                  "urls alice", "probe", "ping", "add nobody-here",
                  'call PrivilegesProtocol GetPrivilegeRemainDuration {"privilegeID": 1}',
                  "accept ghost", "call", "block alice", "blocked", "list",
                  "unblock alice", "blocked", "quit"]
        with contextlib.redirect_stdout(out):
            for line in script:
                if game.onecmd(game.precmd(line)):
                    break
        text = out.getvalue()
        self.assertIn("friends: alice sent a friend request", text)
        self.assertRegex(text, r"alice\s+online\s+co-op\?")
        self.assertRegex(text, r"alice\s+friend\s+online")
        self.assertIn("udp:/address=127.0.0.1", text)
        self.assertIn('"seconds": -1', text)
        self.assertIn("nobody called ghost", text)
        self.assertRegex(text, r"alice\s+pid 0x%X" % alice.pid)
        # Unblocking is a request by name, so alice now has one waiting from bob.
        self.assertEqual(alice.relationships(), {"bob": (3, True)})
        self.assertEqual(alice.call("FriendsProtocol", "GetDetailedList", {
            "byRelationship": 3})["lstFriendsList"], [])

    def test_sessions_clans_stats_and_maps_typed_by_hand(self):
        tool = load("fake_game")
        alice = self.player("alice")
        bob = self.player("bob")
        game = tool.FakeGame(bob)
        out = io.StringIO()
        game.stdout = out

        def told():
            """Wait for what alice did to reach bob, so that the next command shows it."""
            deadline = time.monotonic() + 5
            while bob.secure.requests.empty() and time.monotonic() < deadline:
                time.sleep(0.02)

        G = "GameSessionProtocol"
        session = {}

        def alice_hosts_and_invites():
            key = alice.call(G, "CreateSession", {"gameSession": {"m_typeID": 1, "m_attributes": [
                {"m_ID": 3, "m_value": 4}, {"m_ID": 4, "m_value": 0}]}})["gameSessionKey"]
            alice.call(G, "AddParticipants", {"gameSessionKey": key,
                                              "publicParticipantIDs": [alice.pid]})
            alice.call(G, "SendInvitation", {"invitation": {
                "m_sessionKey": key, "m_recipientPIDs": [bob.pid], "m_message": "co-op?"}})
            session.update(key)
            told()

        def alice_founds_a_clan_and_invites():
            clan = alice.call("ClansProtocol", "CreateClan", {"name": "Wolves", "tag": "WLF",
                                                              "description": ""})["clanId"]
            alice.call("ClansProtocol", "InviteToClan", {"pid": bob.pid, "inviteType": 0})
            session["clan"] = clan

        def alice_accepts_bob():
            alice.call("FriendsProtocol", "AcceptFriendship", {"uiPlayer": bob.pid})
            told()

        script = ["sessions", "sessions search", alice_hosts_and_invites, "sessions search",
                  "sessions invitations", "sessions accept-invite", "sessions", "sessions leave",
                  "sessions host 2 1", "sessions invite alice ready?", "sessions",
                  "sessions leave", "sessions accept-invite", "sessions join 999",
                  "sessions what",
                  "add alice", alice_accepts_bob,
                  "clan", alice_founds_a_clan_and_invites, "clan join", "clan", "clan members",
                  "clan leave", "clan create Ghosts GHO the quiet ones", "clan create Again AGN",
                  "clan invite nobody-here", "clan members 999",
                  "stats read 1", "stats send 1 3 100", "stats send 1 3 5 add",
                  "stats send 2/4 1 7 MAX", "stats read 1", "stats read 2", "stats read 1 alice",
                  "stats send 1 x 1",
                  "maps categories", "maps list 0x4010", "maps",
                  "quit"]
        with contextlib.redirect_stdout(out):
            for step in script:
                if callable(step):
                    step()
                elif game.onecmd(game.precmd(step)):
                    break
        text = out.getvalue()
        sid = session["m_sessionID"]
        # Sessions: found, the invitation shown as it arrives, listed, accepted and left.
        self.assertIn("in no session", text)
        self.assertRegex(text, r"session %d\s+host alice\s+public 1/4 private 0/0" % sid)
        self.assertIn("invitation: alice asks you into session %d: 'co-op?'" % sid, text)
        self.assertRegex(text, r"1\. alice\s+session %d\s+co-op\?" % sid)
        self.assertIn("accepted alice's invitation", text)
        self.assertIn("left session %d" % sid, text)
        self.assertRegex(text, r"hosting session \d+ \(2 public, 1 private\)")
        self.assertIn("invited alice to session", text)
        self.assertIn("abandoned session", text)
        self.assertIn("no invitation to accept", text)
        self.assertIn("refused: GameSessionProtocol.GetSession failed with 0x80120003", text)
        self.assertIn("sessions [host|search|join", text)
        # Alice was told of the acceptance (7007) and of bob's invitation (7005), which bob's
        # abandoning then took back.
        kinds = []
        while not alice.secure.requests.empty():
            kinds.append(alice.next_server_call()[2]["oEvent"]["m_uiType"])
        self.assertEqual([k for k in kinds if k // 1000 == 7], [7007, 7005])
        self.assertEqual(alice.call(G, "GetInvitationsReceived", {"gameSessionTypeID": 1}),
                         {"invitations": []})
        # Friends: the change bob is told of when alice accepts.
        self.assertIn("friends: alice changed: came, went, accepted, removed or blocked", text)
        # Clans.
        self.assertIn("in no clan", text)
        self.assertIn("joined clan %d" % session["clan"], text)
        self.assertIn("clan %d [WLF]" % session["clan"], text)
        self.assertRegex(text, r"alice\s+leader\s+online")
        self.assertRegex(text, r"bob\s+newcomer\s+online")
        self.assertIn("left the clan", text)
        self.assertRegex(text, r"founded clan \d+")
        self.assertIn("refused: 0x325 already in a clan", text)
        self.assertIn("refused: 0x327 no such player", text)
        self.assertIn("nobody", text)
        # Stats: an ADD on top of a SET, a row with contexts, nobody else's.
        self.assertIn("nothing stored", text)
        self.assertIn("sent: board 1 stat 3 ADD 5", text)
        self.assertIn("board 1 []: 3=105", text)
        self.assertIn("board 2 [4]: 1=7", text)
        self.assertIn("error: invalid literal", text)
        # Maps.
        self.assertIn("0x1c013 Conflict", text)
        self.assertRegex(text, r"[0-9a-f]{8} Pipeline Conflict")
        self.assertIn("maps categories | maps list MASK", text)


if __name__ == "__main__":
    unittest.main()
