"""Every method the server answers, called once with nothing filled in, over real sockets.

The other live tests call each method the way the game does. This one calls every method of
every installed service with default values - zeros, empty strings, empty lists - which is what
a game sends when its own state is empty, and what no hand-written test thinks of. Each call
must be answered, with a result or with an RMC error, and must leave no error in the log: a
handler that raises is answered CORE_UNKNOWN, which the client cannot tell from a refusal, and
this is where such a handler shows.

It also holds the list of what is not answered on purpose (grfs.services.NOT_ANSWERED) to the
schema: every method the game's schema has is either answered or listed with its reason.
"""

from grfs import fakegame, protocols
from grfs.fakegame import RmcFailure
from grfs.services import NOT_ANSWERED, not_answered

from tests.live import LiveTest


class Sweep(LiveTest):
    def setUp(self):
        super().setUp()
        self.alice = self.player("alice")
        self.bob = self.player("bob")

    def handled(self):
        """(protocol name, method id, method name) of everything the server answers."""
        services = self.server.backend.router.services.values()
        return [(s.protocol_name, mid, name) for s in sorted(services, key=lambda s: s.protocol_id)
                for mid, name in s.handled()]

    def test_every_method_answers_default_arguments(self):
        calls = self.handled()
        self.assertGreater(len(calls), 140)
        for who in (self.alice, self.bob):
            for protocol, mid, name in calls:
                with self.subTest(player=who.name, method="%s.%s" % (protocol, name)):
                    before = len(self.server.problems())
                    try:
                        who.call(protocol, mid, {})
                    except RmcFailure:
                        pass                     # refusing empty arguments is allowed
                    self.assertEqual(self.server.problems()[before:], [])
        # And the server is no worse for it: both still answered, both still listed.
        self.assertEqual(self.bob.relationships(), {})
        self.assertEqual(sorted(self.on_server(lambda: [
            c.name for c in self.server.backend.clients if c.name])), ["alice", "bob"])

    def test_what_is_not_answered_is_refused_with_its_reason(self):
        reply = self.alice.call_raw("AccountManagementProtocol", 1)
        self.assertFalse(reply.success)
        self.assertTrue(self.logged("not answered, on purpose: the client turns to it"))
        reply = self.alice.call_raw("MiscProtocol", 2)
        self.assertFalse(reply.success)
        self.assertTrue(self.logged("SendGenericEvent: its only task creator"))
        self.assertFalse(self.logged("not in the schema", level=30))


class NotAnswered(LiveTest):
    def test_every_method_in_the_schema_is_answered_or_listed(self):
        router = self.server.backend.router
        by_name = {s.protocol_name: s for s in router.services.values()}
        missing = []
        for pname, proto in sorted(fakegame.schema().protocols.items()):
            service = by_name.get(pname)
            answered = {mid for mid, _ in service.handled()} if service else set()
            for mid, mdef in sorted(proto.methods.items()):
                if mid not in answered and not not_answered(pname, mid):
                    missing.append("%s.%s (%d)" % (pname, mdef.name, mid))
        self.assertEqual(missing, [])

    def test_every_protocol_id_is_served_or_listed(self):
        served = set(self.server.backend.router.services)
        self.assertEqual(sorted("0x%X %s" % (pid, name) for pid, name in protocols.NAMES.items()
                                if pid not in served and not not_answered(name)), [])

    def test_nothing_listed_is_answered_after_all(self):
        """A stale entry would hide a method that is answered behind a reason it no longer
        has."""
        router = self.server.backend.router
        by_name = {s.protocol_name: s for s in router.services.values()}
        stale = []
        for pname, methods, _ in NOT_ANSWERED:
            service = by_name.get(pname)
            if service is None:
                continue
            answered = {mid for mid, _ in service.handled()}
            if methods is None or answered & set(methods):
                stale.append(pname)
        self.assertEqual(stale, [])
