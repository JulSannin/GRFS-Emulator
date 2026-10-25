"""Signing in against a running server, the whole way, as the game does it."""

import os

from grfs.fakegame import (PLAYER_VPORT, Connection, RmcFailure, find_method, read_ticket,
                           schema)
from grfs.kerberos import PASSWORD_DEFAULT, SERVER_PID, TRACKING_PID
from grfs.protocols import TICKET_GRANTING
from grfs.rmc.router import SUCCESS, Errors
from grfs.services.auth import BUILD, LOGINEX_TAIL

from tests.live import LiveTest


class SigningIn(LiveTest):
    def test_the_configuration_points_at_the_authentication_server(self):
        values = self.server.config_service()
        self.assertEqual(values["SandboxUrl"],
                         "prudp:/address=127.0.0.1;port=%d" % self.server.ports["auth"])

    def test_the_login_reply_is_what_the_client_checks(self):
        alice = self.player("alice")
        reply = alice.login_reply
        self.assertEqual(reply["retval"], SUCCESS)
        self.assertEqual(alice.pid, 0x1234)           # the first account on a fresh server
        self.assertEqual(reply["strReturnMsg"], BUILD)
        # Past the end of what the schema declares, the four bytes the live servers sent.
        self.assertEqual(reply["__trailing__"], LOGINEX_TAIL)
        self.assertEqual(reply["pConnectionData"]["m_lstSpecialProtocols"], [])
        self.assertEqual(alice.secure_url,
                         "prudps:/address=127.0.0.1;port=%d;CID=1;PID=%d;sid=1;stream=3;type=2"
                         % (self.server.ports["secure"], SERVER_PID))

    def test_the_ticket_decrypts_and_is_the_same_when_asked_for_again(self):
        alice = self.player("alice")
        session_key, server_pid, blob = read_ticket(alice.ticket, alice.pid, PASSWORD_DEFAULT)
        self.assertEqual(server_pid, SERVER_PID)
        self.assertEqual(len(session_key), 16)
        self.assertTrue(blob)
        self.assertEqual(alice.requested_ticket, alice.ticket)

    def test_the_secure_server_answers_the_challenge_with_one_more(self):
        alice = self.player("alice")
        code, answer = alice.challenge
        self.assertEqual(answer, code + 1)

    def test_registering_hands_out_connection_ids_from_78(self):
        alice = self.player("alice")
        bob = self.player("bob")
        self.assertEqual((alice.rvcid, bob.rvcid), (78, 79))
        self.assertEqual(alice.register_reply["retval"], SUCCESS)
        url = alice.register_reply["urlPublic"]
        self.assertIn("address=127.0.0.1", url)
        self.assertIn("PID=%d" % alice.pid, url)
        self.assertIn("RVCID=78", url)
        record = self.client_of(alice)
        self.assertEqual(record.name, "alice")
        self.assertEqual(record.urls, ["prudp:/address=127.0.0.1;port=2347;sid=15;type=2"])

    def test_a_returning_player_keeps_their_pid_whatever_the_case(self):
        first = self.player("Alice")
        pid = first.pid
        first.close()
        again = self.player("alice")
        self.assertEqual(again.pid, pid)
        self.assertEqual(self.player("bob").pid, pid + 1)

    def test_telemetry_signs_in_beside_the_player_without_taking_over(self):
        alice = self.player("alice")
        self.assertEqual(alice.login_telemetry(), TRACKING_PID)
        record = self.client_of(alice)
        self.assertEqual((record.name, record.pid), ("alice", alice.pid))
        self.assertEqual(record.urls, ["prudp:/address=127.0.0.1;port=2347;sid=15;type=2"])
        self.assertNotIn("Tracking", [a.name for a in self.server.backend.accounts.by_name.values()])
        # And the player's own connection still works after it.
        self.assertTrue(alice.call("PrivilegesProtocol", "GetPrivilegesEx", {"localeCode": "en"}))

    def test_logging_in_again_from_a_new_port_forgets_the_old_one(self):
        first = self.player("alice")
        first.close()
        again = self.player("alice")
        endpoints = self.on_server(lambda: [c.endpoint for c in self.server.backend.clients
                                            if c.pid == again.pid])
        self.assertEqual(endpoints, [again.wire.address])

    def test_the_credentials_are_neither_logged_nor_stored(self):
        alice = self.player("alice")
        alice.call("FriendsProtocol", "GetRelationships", {"resultRange": {"m_uiSize": 10}})
        self.on_server(self.server.backend.accounts.save)
        for line in self.server.capture.lines():
            self.assertNotIn(alice.PASSWORD, line)
            self.assertNotIn(alice.ONLINE_KEY, line)
        for name in os.listdir(self.server.data_dir):
            with open(os.path.join(self.server.data_dir, name), encoding="utf-8") as f:
                text = f.read()
            self.assertNotIn(alice.PASSWORD, text)
            self.assertNotIn(alice.ONLINE_KEY, text)


class Refusals(LiveTest):
    """Calls that must fail - and fail with an answer, because silence makes the game resend."""

    def test_a_login_with_no_name_is_refused(self):
        wire = self.server.wire()
        auth = Connection(wire, ("127.0.0.1", self.server.ports["auth"]))
        auth.handshake()
        mdef = find_method(schema(), "TicketGrantingProtocol", "LoginEx")
        reply = auth.call(TICKET_GRANTING, mdef.id, schema().encode_request(mdef, {
            "strUserName": "", "oExtraData": None}))
        self.assertFalse(reply.success)
        self.assertEqual(reply.error, Errors.RV_INVALID_USERNAME)

    def test_a_ticket_for_a_connection_that_never_logged_in_is_refused(self):
        wire = self.server.wire()
        auth = Connection(wire, ("127.0.0.1", self.server.ports["auth"]))
        auth.handshake()
        mdef = find_method(schema(), "TicketGrantingProtocol", "RequestTicket")
        reply = auth.call(TICKET_GRANTING, mdef.id, schema().encode_request(
            mdef, {"idSource": 0x1234, "idTarget": SERVER_PID}))
        self.assertEqual((reply.success, reply.error), (False, Errors.RV_NOT_AUTHENTICATED))

    def test_registering_without_a_login_is_refused(self):
        stranger = self.player("nobody", login=False)
        stranger.secure = Connection(stranger.wire, ("127.0.0.1", self.server.ports["secure"]))
        stranger.secure.handshake()
        with self.assertRaises(RmcFailure) as caught:
            stranger.register()
        self.assertEqual(caught.exception.code, Errors.RV_NOT_AUTHENTICATED)
        with self.assertRaises(RmcFailure) as caught:
            stranger.call("FriendsProtocol", "GetRelationships", {})
        self.assertEqual(caught.exception.code, Errors.RV_NOT_AUTHENTICATED)

    def test_a_ticket_sealed_with_the_wrong_key_gets_the_wrong_answer(self):
        alice = self.player("alice", login=False)
        alice.login(register=False)
        code = 0x01020304
        answer = alice.secure.connect_with_ticket(alice.ticket, b"\x00" * 16, code)
        self.assertNotEqual(answer, code + 1)

    def test_a_ticket_on_a_connection_that_never_logged_in_gets_no_answer(self):
        alice = self.player("alice")
        other = Connection(alice.wire, ("127.0.0.1", self.server.ports["secure"]), vport=0x3D)
        other.handshake()
        with self.assertRaises(ValueError):
            other.connect_with_ticket(alice.ticket, alice.session_key)
        self.assertTrue(self.logged("nothing logged in there"))

    def test_a_protocol_with_no_service_is_answered_with_an_error(self):
        # AccountManagement is one the game has but this server does not answer, on purpose
        # (see grfs.services): the client only falls back to it after a login error.
        alice = self.player("alice")
        reply = alice.call_raw("AccountManagementProtocol", 1)
        self.assertEqual((reply.success, reply.error), (False, Errors.CORE_UNKNOWN))
        self.assertTrue(self.logged("no service"))

    def test_a_method_the_schema_does_not_have_is_answered_with_an_error(self):
        alice = self.player("alice")
        reply = alice.call_raw("FriendsProtocol", 99)
        self.assertEqual((reply.success, reply.error), (False, Errors.CORE_INVALID_ARGUMENT))

    def test_a_method_with_no_handler_is_answered_with_an_error(self):
        # GetAccount has no caller in the client (it is in the list of methods not answered on
        # purpose), so it is the example of a method the schema has and no handler answers.
        alice = self.player("alice")
        with self.assertRaises(RmcFailure) as caught:
            alice.call("UbiAccountManagementProtocol", "GetAccount", {})
        self.assertEqual(caught.exception.code, Errors.CORE_UNKNOWN)
        self.assertTrue(self.logged("not implemented"))

    def test_parameters_that_do_not_decode_are_answered_with_an_error(self):
        self.expect_errors = True
        alice = self.player("alice")
        mdef = find_method(schema(), "FriendsProtocol", "AddFriendByName")
        reply = alice.call_raw("FriendsProtocol", mdef.id, b"\x09")
        self.assertEqual((reply.success, reply.error), (False, Errors.CORE_INVALID_ARGUMENT))
        self.assertTrue(self.logged("cannot decode the request"))
        # And the server is no worse for it.
        self.assertTrue(alice.call("SearchFriendsProtocol", "SearchFriends", {"partOfName": ""}))


class FindingOtherPlayers(LiveTest):
    def test_where_to_reach_another_player(self):
        alice = self.player("alice", urls=["prudp:/address=10.0.0.2;port=2347;sid=15;type=2",
                                           "prudp:/address=192.168.1.2;port=2347;sid=15;type=2"])
        bob = self.player("bob")
        answer = bob.call("SecureConnectionProtocol", "RequestURLs",
                          {"cidTarget": 0, "pidTarget": alice.pid})
        self.assertEqual(answer, {"retval": True, "plstURLs": alice.urls})
        by_cid = bob.call("SecureConnectionProtocol", "RequestURLs",
                          {"cidTarget": alice.rvcid, "pidTarget": 0})
        self.assertEqual(by_cid["plstURLs"], alice.urls)
        data = bob.call("SecureConnectionProtocol", "RequestConnectionData",
                        {"cidTarget": 0, "pidTarget": alice.pid})
        self.assertTrue(data["retval"])
        self.assertEqual([d["m_StationUrl"] for d in data["pvecConnectionsData"]], alice.urls)
        self.assertEqual({d["m_ConnectionID"] for d in data["pvecConnectionsData"]},
                         {alice.rvcid})

    def test_somebody_who_is_not_here_cannot_be_reached(self):
        bob = self.player("bob")
        self.assertEqual(bob.call("SecureConnectionProtocol", "RequestURLs",
                                  {"cidTarget": 0, "pidTarget": 0x9999}),
                         {"retval": False, "plstURLs": []})
        self.assertEqual(bob.call("SecureConnectionProtocol", "RequestConnectionData",
                                  {"cidTarget": 4242, "pidTarget": 0}),
                         {"retval": False, "pvecConnectionsData": []})

    def test_the_older_register_call_works_too(self):
        alice = self.player("alice", login=False)
        alice.login(register=False)
        answer = alice.call("SecureConnectionProtocol", "Register",
                            {"vecMyURLs": ["prudp:/address=10.1.1.1;port=2347"]})
        self.assertEqual(answer["retval"], SUCCESS)
        self.assertEqual(answer["pidConnectionID"], 78)
        self.assertEqual(self.client_of(alice).urls, ["prudp:/address=10.1.1.1;port=2347"])

    def test_the_servers_own_url_registered_back_is_left_out(self):
        """A second registration from the same game carries the urlPublic the first one was
        answered with; nobody reaches the game there."""
        alice = self.player("alice")
        own = "prudp:/address=10.1.1.1;port=2347;sid=15;type=2"
        alice.register([alice.register_reply["urlPublic"], own])
        self.assertEqual(self.client_of(alice).urls, [own])
        self.assertTrue(self.logged("the server's own URL(s) back as its own"))


class Privileges(LiveTest):
    def test_the_privileges_arrive_whole_although_they_take_several_packets(self):
        alice = self.player("alice")
        rows = alice.call("PrivilegesProtocol", "GetPrivilegesEx", {"localeCode": "en"})
        ids = [p["m_ID"] for p in rows["privilegesEx"]]
        self.assertIn(1, ids)                          # "allow to play online"
        self.assertEqual(len(ids), len(set(ids)))
        self.assertTrue(all(p["m_duration"] == -1 for p in rows["privilegesEx"]))
        self.assertTrue(self.logged("sending") and self.logged("fragments"))

    def test_the_other_privilege_calls(self):
        alice = self.player("alice")
        old = alice.call("PrivilegesProtocol", "GetPrivileges", {"localeCode": "en"})
        self.assertEqual(dict(old["privileges"])[1]["m_description"], "Allow to play online")
        self.assertEqual(alice.call("PrivilegesProtocol", "GetExpiredPrivileges"),
                         {"expiredPrivileges": []})
        self.assertEqual(alice.call("PrivilegesProtocol", "GetPrivilegeRemainDuration",
                                    {"privilegeID": 1}), {"seconds": -1})

    def test_a_second_login_still_gets_them(self):
        """The bug that found the fragment numbering: from the second login on, the reply was
        numbered past the client's window and never delivered."""
        for _ in range(3):
            alice = self.player("alice")
            self.assertTrue(alice.call("PrivilegesProtocol", "GetPrivilegesEx",
                                       {"localeCode": "en"})["privilegesEx"])
            self.assertFalse(alice.secure.stuck())
            alice.close()
