"""Start everything: `python -m grfs`.

One event loop runs every listener - the configuration service over TCP, the two PRUDP servers
and the NAT type detector over UDP, and the status page. Nothing here blocks, so a slow answer
anywhere cannot make the game start retransmitting, which is the failure mode this design is
built to avoid.
"""

import argparse
import asyncio
import sys

from . import log as logs
from .backend import Backend
from .config import Config
from .net import PrudpServer
from .onlineconfig import OnlineConfigServer
from .panel import Panel
from .punch import PunchResponder


# How often housekeeping looks. It decides how late a friend's screen learns that a player went
# quiet, on top of the presence window itself.
HOUSEKEEPING_EVERY = 10


async def housekeeping(backend) -> None:
    """Tell friends about players who went quiet, and forget records of games that have gone
    for good. Nothing else here is on a timer: the server only ever answers, so there is no
    other state that needs stirring."""
    while True:
        await asyncio.sleep(HOUSEKEEPING_EVERY)
        try:
            backend.notice_departures()
            backend.forget_quiet_clients()
        except Exception:
            logs.get("main").exception("housekeeping failed; trying again in %d s",
                                       HOUSEKEEPING_EVERY)


class Running:
    """A started server: the backend, its listeners, and the ports they actually got.

    The ports are read back from the sockets rather than from the settings, because a test asks
    for port 0 - any free one - and has to learn where it landed. The authentication server's is
    also written back into the backend: the configuration service and the login hand it out.
    """

    def __init__(self, backend, transports, online_config, panel, sweeper, punch=None):
        self.backend = backend
        self.transports = transports
        self.online_config = online_config
        self.panel = panel
        self.sweeper = sweeper
        self.punch = punch

    @property
    def ports(self) -> dict:
        """`punch` is the first of the detector's two ports - the second is always the one above
        it - and 0 when it is switched off."""
        return {"onlineconfig": self.online_config.port, "auth": self.backend.auth_port,
                "secure": self.backend.secure_port,
                "punch": self.punch.port if self.punch is not None else 0,
                "panel": self.panel.port if self.panel.server is not None else 0}

    def describe(self) -> str:
        return ", ".join("%s=%s" % kv for kv in self.ports.items())

    async def stop(self) -> None:
        self.sweeper.cancel()
        for transport in self.transports:
            transport.close()
        if self.punch is not None:
            self.punch.close()
        self.online_config.close()
        self.panel.close()
        for server in (self.online_config.server, self.panel.server):
            if server is not None:
                await server.wait_closed()
        self.backend.save()
        logs.get("main").info("stopped")


async def start(config: Config, data_dir: str = None, any_port: bool = False) -> Running:
    """Bring up every listener and return once they are all bound.

    `any_port` binds every listener to free ports instead of the configured ones - for the
    test harness, which must be able to run beside a real server. The game itself cannot use
    that: it expects the secure server one port above the authentication server. A punch_port
    of 0 switches the NAT type detector off either way.

    The detector is up before the configuration service, which names it: the game asks for
    the configuration once, at launch, and starts detecting straight away.
    """
    backend = Backend(config, data_dir=data_dir)
    loop = asyncio.get_running_loop()
    bind = config.get("bind", "0.0.0.0")

    transports = []
    for name in ("auth", "secure"):
        port = 0 if any_port else getattr(backend, name + "_port")
        transport, server = await loop.create_datagram_endpoint(
            lambda n=name, p=port: PrudpServer(backend, n, p), local_addr=(bind, port))
        server.port = transport.get_extra_info("sockname")[1]
        setattr(backend, name + "_port", server.port)
        backend.servers[name] = server
        transports.append(transport)
        logs.get(name).info("listening on %s:%d (UDP)", bind, server.port)

    punch = None
    if config.int("punch_port", 21032):
        punch = PunchResponder(backend)
        try:
            await punch.start(config.int("punch_port", 21032), bind, any_port=any_port)
        except OSError:
            for transport in transports:
                transport.close()
            raise
        backend.servers["punch"] = punch

    online_config = OnlineConfigServer(backend)
    await online_config.start(0 if any_port else None)
    panel = Panel(backend)
    await panel.start(0 if any_port else None)

    sweeper = asyncio.create_task(housekeeping(backend))
    return Running(backend, transports, online_config, panel, sweeper, punch)


async def serve(config: Config) -> None:
    log = logs.get("main")
    running = await start(config)
    log.info("ready: %s", running.describe())
    log.info("each machine that plays needs onlineconfigservice.ubi.com in its hosts file,"
             " pointing at this server")
    try:
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        pass
    finally:
        await running.stop()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="grfs", description=__doc__.splitlines()[0])
    parser.add_argument("--bind", help="address to listen on (default 0.0.0.0)")
    parser.add_argument("--advertise", metavar="ADDRESS",
                        help="the address to hand clients, when the one we would work out"
                             " ourselves is not reachable (a NAT in front, say)")
    parser.add_argument("--log-level", choices=("trace", "debug", "info", "warning", "error"),
                        help="'trace' logs every packet")
    parser.add_argument("--auth-port", type=int,
                        help="authentication server port (default 21030); the game expects the"
                             " secure server one above it")
    parser.add_argument("--secure-port", type=int, help="secure server port (default 21031)")
    parser.add_argument("--punch-port", type=int,
                        help="NAT type detection, on this UDP port and the next (default 21032);"
                             " 0 to switch it off")
    parser.add_argument("--onlineconfig-port", type=int,
                        help="configuration service port (default 80)")
    parser.add_argument("--panel-port", type=int, help="status page port, 0 to switch it off")
    parser.add_argument("--config", metavar="FILE", help="an ini file to read instead of grfs.ini")
    parser.add_argument("--data-dir", metavar="DIR",
                        help="where accounts, friends, clans and stats are kept (default data/)")
    args = parser.parse_args(argv)

    overrides = {k: str(v) for k, v in (("bind", args.bind), ("advertise", args.advertise),
                                        ("data_dir", args.data_dir),
                                        ("log_level", args.log_level),
                                        ("auth_port", args.auth_port),
                                        ("secure_port", args.secure_port),
                                        ("punch_port", args.punch_port),
                                        ("onlineconfig_port", args.onlineconfig_port),
                                        ("panel_port", args.panel_port)) if v is not None}
    config = Config(path=args.config, overrides=overrides)
    logs.setup(config.get("log_level", "info"), config.get("log_file"))
    log = logs.get("main")
    if config.path:
        log.info("settings from %s", config.path)

    try:
        asyncio.run(serve(config))
    except KeyboardInterrupt:
        pass
    except OSError as e:
        # The usual one: port 80 is taken, or another copy is already running.
        log.error("cannot start: %s", e)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
