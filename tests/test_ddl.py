import datetime
import os
import unittest

from grfs.rmc import message
from grfs.rmc.ddl import Schema, parse_type
from grfs.rmc.stream import Reader, Writer, pack_datetime, unpack_datetime

SCHEMA = os.path.join(os.path.dirname(__file__), "..", "schema", "grfs-ddl.json")


def hx(text):
    return bytes.fromhex(text)


class DdlCodec(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.s = Schema().load_ddl_json(SCHEMA)

    def test_schema_has_the_game_session_protocol(self):
        m = self.s.method("GameSessionProtocol", 1)
        self.assertEqual(m.name, "CreateSession")
        self.assertEqual([p.name for p in m.inputs], ["gameSession"])
        self.assertEqual([p.name for p in m.outputs], ["gameSessionKey"])
        self.assertEqual(self.s.method("GameSessionProtocol", 24).name,
                         "SearchSessionsWithParticipants")
        self.assertEqual(len(self.s.protocols), 27)

    def test_captured_create_session_decodes(self):
        # GameSessionService method 1 parameters as captured from the game (README journal).
        body = hx("01 00 00 00 05 00 00 00 07 00 00 00 01 00 00 00 03 00 00 00 06 00 00 00"
                  " 04 00 00 00 00 00 00 00 66 00 00 00 00 00 00 00 64 00 00 00 01 00 00 00")
        args = self.s.decode_request(self.s.method("GameSessionProtocol", 1), body)
        gs = args["gameSession"]
        self.assertEqual(gs["m_typeID"], 1)
        self.assertEqual([(a["m_ID"], a["m_value"]) for a in gs["m_attributes"]],
                         [(7, 1), (3, 6), (4, 0), (102, 0), (100, 1)])
        self.assertNotIn("__trailing__", args)

    def test_captured_search_with_participants_decodes(self):
        args = self.s.decode_request(self.s.method("GameSessionProtocol", 24),
                                     hx("01 00 00 00 00 00 00 00"))
        self.assertEqual(args, {"gameSessionTypeID": 1, "participantIDs": []})

    def test_inherited_fields_come_first(self):
        m = self.s.method("GameSessionProtocol", 24)
        result = {"searchResults": [{
            "m_sessionKey": {"m_typeID": 1, "m_sessionID": 9}, "m_hostPID": 0x1234,
            "m_hostURLs": ["prudp:/address=1.2.3.4;port=2347"],
            "m_attributes": [{"m_ID": 3, "m_value": 4}], "m_participantIDs": [0x1234]}]}
        data = self.s.encode_response(m, result)
        r = Reader(data)
        self.assertEqual(r.u32(), 1)                 # one result
        self.assertEqual((r.u32(), r.u32()), (1, 9))  # GameSessionKey
        self.assertEqual(r.u32(), 0x1234)            # host pid
        back = self.s.read(Reader(data), parse_type("qlist<GameSessionSearchWithParticipantsResult>"))
        self.assertEqual(back, result["searchResults"])

    def test_credentials_are_skipped_not_decoded(self):
        inner = Writer().string("tester").string("").string("dummy").bytes()
        any_data = (Writer().string("UbiAuthenticationLoginCustomData").u32(len(inner) + 4)
                    .u32(len(inner)).raw(inner).bytes())
        v = self.s.read(Reader(any_data), parse_type("any<Data,string>"))
        self.assertEqual(v, {"m_userName": "tester",
                             "__class__": "UbiAuthenticationLoginCustomData"})

    def test_rmc_request_framing(self):
        msg = message.parse(hx("11 00 00 00 8A 0D 00 00 00 03 00 00 00 34 12 00 00 00 10 00 00"))
        self.assertEqual((msg.protocol, msg.is_request, msg.call_id, msg.method),
                         (0x0A, True, 13, 3))
        args = self.s.decode_request(self.s.method("TicketGrantingProtocol", 3), msg.body)
        self.assertEqual(args, {"idSource": 0x1234, "idTarget": 0x1000})

    def test_rmc_response_framing_round_trip(self):
        raw = message.response(0x138B, 5, 1, b"\x01\x02")
        msg = message.parse(raw)
        self.assertEqual((msg.protocol, msg.is_request, msg.success, msg.call_id, msg.method,
                          msg.body), (0x138B, False, True, 5, 1, b"\x01\x02"))
        err = message.parse(message.error(0x2A, 7, 0x80010001))
        self.assertEqual((err.success, err.error, err.call_id), (False, 0x80010001, 7))

    def test_datetime_packing(self):
        t = datetime.datetime(2026, 10, 24, 18, 30, 5)
        self.assertEqual(unpack_datetime(pack_datetime(t)), t)


if __name__ == "__main__":
    unittest.main()
