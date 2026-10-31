"""Settings: defaults, an optional grfs.ini beside the package, and GRFS_* environment overrides.

Nothing here decides what address a client is told to connect to - that is derived from the
endpoint each client actually reached us on (see Backend.address_for). A config value standing in
for it was the single most expensive bug in the previous attempt: with the server bound to
0.0.0.0 every client was handed "address=0.0.0.0", silently skipped the registration that the
rest of the login hangs off, and dropped to offline mode.
"""

import configparser
import os

from .paths import runtime_dir

DEFAULTS = {
    # The address to listen on. 0.0.0.0 is fine and preferred - what we advertise is worked out
    # per client, not taken from here.
    "bind": "0.0.0.0",
    # Only needed when the address we would work out ourselves is not the one clients can reach
    # (a NAT in front, or several interfaces where the routing choice is wrong).
    "advertise": "",
    "onlineconfig_port": "80",
    "auth_port": "21030",
    "secure_port": "21031",
    # The NAT type detector, on this port and the one above it; 0 switches it off, and then
    # every game's NAT type is Unknown (see grfs.punch).
    "punch_port": "21032",
    "panel_port": "8080",
    "log_level": "info",
    "log_file": "logs/grfs.log",
    # Written into the station URL handed back on registration; the game's P2P socket.
    "game_port": "2347",
    # One message for the main menu's news panel, if either is set; none by default.
    "news_title": "",
    "news_body": "",
    # Two copies of the game on one machine signed in to one Ubisoft account: the second plays
    # as name_2 instead of replacing the first (services.auth.TicketGranting._twin_name).
    "twin_logins": "no",
    # Where accounts, friends, clans and statistics are kept; relative to the project folder.
    "data_dir": "data",
}


class Config:
    def __init__(self, path: str = None, overrides: dict = None):
        self.values = dict(DEFAULTS)
        path = path or str(runtime_dir() / "grfs.ini")
        self.path = path if os.path.exists(path) else None
        if self.path:
            parser = configparser.ConfigParser()
            parser.read(self.path, encoding="utf-8")
            if parser.has_section("server"):
                self.values.update({k: v for k, v in parser.items("server")})
        for key in list(self.values):
            env = os.environ.get("GRFS_" + key.upper())
            if env is not None:
                self.values[key] = env
        self.values.update(overrides or {})

    def get(self, key: str, default: str = "") -> str:
        return str(self.values.get(key, default)).strip()

    def int(self, key: str, default: int = 0) -> int:
        try:
            return int(self.get(key, str(default)), 0)
        except ValueError:
            return default

    def bool(self, key: str, default: bool = False) -> bool:
        return self.get(key, "yes" if default else "no").lower() in ("1", "yes", "true", "on")

    def describe(self) -> str:
        shown = ("bind", "advertise", "onlineconfig_port", "auth_port", "secure_port",
                 "punch_port", "panel_port", "game_port")
        return ", ".join("%s=%s" % (k, self.get(k) or "-") for k in shown)
