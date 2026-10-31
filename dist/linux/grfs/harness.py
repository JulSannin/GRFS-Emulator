"""A real server in a thread, on free ports, for tests that talk to it over real sockets.

    with Harness() as server:
        alice = server.player("alice")
        bob = server.player("bob")
        ...
        server.assert_clean()

It runs the same `start()` as `python -m grfs`, so every listener, every socket and the event
loop are the production ones; only the ports are picked by the system, the address is 127.0.0.1
and the data lives in a temporary directory. Several can run at once and beside a real server.

Every record the server logs is kept, at the packet-trace level, so a test can say "and nothing
went wrong on the way" - no exception, no error - which catches a handler that failed and was
answered with an error code the test did not happen to look at.
"""

import asyncio
import logging
import os
import shutil
import tempfile
import threading

from . import log as logs
from .__main__ import start
from .config import Config
from .fakegame import Peer, Player, Wire, fetch_config


class Capture(logging.Handler):
    def __init__(self):
        super().__init__(logs.TRACE)
        self.records = []
        self.guard = threading.Lock()

    def emit(self, record):
        with self.guard:
            self.records.append(record)

    def lines(self, level: int = logs.TRACE) -> list:
        with self.guard:
            return ["%s %s %s" % (r.levelname, r.name, r.getMessage())
                    for r in self.records if r.levelno >= level]

    def has(self, text: str, level: int = logs.TRACE) -> bool:
        return any(text in line for line in self.lines(level))


class Harness:
    def __init__(self, data_dir: str = None, **settings):
        """`settings` are the server's own (auto_friends="yes", advertise=..., and so on).
        `data_dir` keeps the register between two harnesses, and "" keeps nothing at all; by
        default each gets a fresh directory, removed again on stop."""
        self.own_dir = data_dir is None
        self.data_dir = tempfile.mkdtemp(prefix="grfs-test-") if data_dir is None else data_dir
        values = {"bind": "127.0.0.1", "advertise": "", "log_file": "", "log_level": "trace",
                  "auto_friends": "no"}
        values.update({k: str(v) for k, v in settings.items()})
        self.config = Config(path=os.devnull, overrides=values)
        self.capture = Capture()
        self.loop = None
        self.running = None
        self.thread = None
        self.players = []
        self.wires = []
        self._started = threading.Event()
        self._failure = None

    # ----- life -----------------------------------------------------------------------------
    def start(self) -> "Harness":
        root = logging.getLogger("grfs")
        self._saved = (root.level, root.propagate)
        root.setLevel(logs.TRACE)
        root.propagate = False
        root.addHandler(self.capture)
        self.thread = threading.Thread(target=self._run, name="grfs server", daemon=True)
        self.thread.start()
        if not self._started.wait(10):
            raise TimeoutError("the server did not start")
        if self._failure is not None:
            self._detach()
            raise self._failure
        return self

    def _run(self) -> None:
        self.loop = asyncio.new_event_loop()
        try:
            self.running = self.loop.run_until_complete(
                start(self.config, data_dir=self.data_dir, any_port=True))
        except BaseException as e:
            self._failure = e
            self._started.set()
            self.loop.close()
            return
        self._started.set()
        try:
            self.loop.run_forever()
        finally:
            self.loop.run_until_complete(self.running.stop())
            self.loop.close()

    def stop(self) -> None:
        for player in self.players:
            player.close()
        for wire in self.wires:
            wire.close()
        self.players.clear()
        self.wires.clear()
        if self.loop is not None and self.thread.is_alive():
            self.loop.call_soon_threadsafe(self.loop.stop)
            self.thread.join(10)
        self._detach()
        if self.own_dir:
            shutil.rmtree(self.data_dir, ignore_errors=True)

    def _detach(self) -> None:
        root = logging.getLogger("grfs")
        root.removeHandler(self.capture)
        root.setLevel(self._saved[0])
        root.propagate = self._saved[1]

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()

    # ----- reaching inside ------------------------------------------------------------------
    @property
    def backend(self):
        return self.running.backend

    @property
    def ports(self) -> dict:
        return self.running.ports

    def run(self, fn, *args, timeout: float = 10):
        """Run `fn(*args)` on the server's own thread and return what it returned - the only safe
        way to look at or change server state while it is serving."""
        import concurrent.futures
        done = concurrent.futures.Future()

        def call():
            try:
                done.set_result(fn(*args))
            except BaseException as e:
                done.set_exception(e)
        self.loop.call_soon_threadsafe(call)
        return done.result(timeout)

    # ----- game stand-ins -------------------------------------------------------------------
    def wire(self) -> Wire:
        wire = Wire()
        self.wires.append(wire)
        return wire

    def player(self, name: str, login: bool = True, wire: Wire = None, **kwargs) -> Player:
        player = Player("127.0.0.1", self.ports["auth"], self.ports["secure"], name, wire=wire,
                        **kwargs)
        self.players.append(player)
        if login:
            player.login()
        return player

    def peer(self) -> Peer:
        return Peer()

    def config_service(self, **kwargs) -> dict:
        return fetch_config("127.0.0.1", self.ports["onlineconfig"], **kwargs)

    # ----- what happened --------------------------------------------------------------------
    def problems(self) -> list:
        """Errors and exceptions the server logged, and anything the stand-ins found odd."""
        out = [line for line in self.capture.lines(logging.ERROR)]
        for record in list(self.capture.records):
            if record.exc_info and record.levelno < logging.ERROR:
                out.append("exception logged at %s: %s" % (record.levelname,
                                                           record.getMessage()))
        for wire in self.wires + [p.wire for p in self.players]:
            out.extend(wire.errors)
            for connection in list(wire.connections.values()):
                if connection.bad_signatures:
                    out.append("%d packet(s) to %02X signed wrongly"
                               % (connection.bad_signatures, connection.vport))
                if connection.stuck():
                    out.append("reliable packets stuck behind a gap on %02X: %s"
                               % (connection.vport, sorted(connection.held)))
        return out

    def assert_clean(self) -> None:
        problems = self.problems()
        if problems:
            raise AssertionError("the server or a client had trouble:\n  " +
                                 "\n  ".join(problems[:20]))
