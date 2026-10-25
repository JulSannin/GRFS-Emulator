"""The base of the tests that run a real server and talk to it over real sockets."""

import time
import unittest

from grfs.harness import Harness


class LiveTest(unittest.TestCase):
    """A fresh server for every test, stopped afterwards, and checked for trouble on the way.

    A test that provokes an error on purpose sets `expect_errors` - otherwise any error the
    server logged, any exception, any packet signed for the wrong connection and any reliable
    packet stuck behind a gap fails the test, whatever else it asserted.
    """

    settings = {}
    expect_errors = False

    def setUp(self):
        self.server = Harness(**self.settings).start()
        self.addCleanup(self.server.stop)

    def tearDown(self):
        if not self.expect_errors:
            self.server.assert_clean()

    # ----- shortcuts --------------------------------------------------------------------------
    def player(self, name, **kwargs):
        return self.server.player(name, **kwargs)

    def on_server(self, fn, *args):
        return self.server.run(fn, *args)

    def logged(self, text, level=0):
        return self.server.capture.has(text, level)

    def wait_logged(self, text, timeout=5.0):
        """Whether the server logs this within `timeout` - for what it does after answering."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.logged(text):
                return True
            time.sleep(0.02)
        return False

    def count_logged(self, text):
        return sum(text in line for line in self.server.capture.lines())

    def client_of(self, player):
        """The server's record of this player's game."""
        return self.on_server(lambda: self.server.backend.clients.by_pid(player.pid))
