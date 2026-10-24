"""Start everything: `python -m grfs`.

One event loop runs all four listeners - the configuration service over TCP, the two PRUDP
servers over UDP, and the status page. Nothing here blocks, so a slow answer anywhere cannot
make the game start retransmitting, which is the failure mode this design is built to avoid.
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


async def serve(config: Config) -> None:
    log = logs.get("main")
    backend = Backend(config)
    loop = asyncio.get_running_loop()
    bind = config.get("bind", "0.0.0.0")

    for name, port in (("auth", backend.auth_port), ("secure", backend.secure_port)):
        _, server = await loop.create_datagram_endpoint(
            lambda n=name, p=port: PrudpServer(backend, n, p), local_addr=(bind, port))
        backend.servers[name] = server
        logs.get(name).info("listening on %s:%d (UDP)", bind, port)

    online_config = OnlineConfigServer(backend)
    await online_config.start()
    panel = Panel(backend)
    await panel.start()

    log.info("ready: %s", config.describe())
    log.info("each machine that plays needs onlineconfigservice.ubi.com in its hosts file,"
             " pointing at this server")
    try:
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        pass
    finally:
        online_config.close()
        panel.close()
        backend.accounts.save()
        log.info("stopped")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="grfs", description=__doc__.splitlines()[0])
    parser.add_argument("--bind", help="address to listen on (default 0.0.0.0)")
    parser.add_argument("--advertise", metavar="ADDRESS",
                        help="the address to hand clients, when the one we would work out"
                             " ourselves is not reachable (a NAT in front, say)")
    parser.add_argument("--log-level", choices=("trace", "debug", "info", "warning", "error"),
                        help="'trace' logs every packet")
    parser.add_argument("--panel-port", type=int, help="status page port, 0 to switch it off")
    parser.add_argument("--config", metavar="FILE", help="an ini file to read instead of grfs.ini")
    args = parser.parse_args(argv)

    overrides = {k: str(v) for k, v in (("bind", args.bind), ("advertise", args.advertise),
                                        ("log_level", args.log_level),
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
