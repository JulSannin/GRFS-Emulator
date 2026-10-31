"""MiscProtocol (0x6F): the match key, the server's tuning switches, privilege trials.

    1 GetMatchUniqueKey      a matchmade host, after GetMapList, before the lobby is created
    3 GetGenericSettings     the front end, cached by the client for 15 minutes
    4 UnlockPrivilegeTrial   sign-in and Uplay rewards, for a privilege the player lacks
    5 UnlockPrivilegeTrial2  the same, for privilege ids 3000..3500 (the name is ours)

Method 2 (SendGenericEvent) has a request stub and no caller anywhere in the game script, so it
is not declared here and gets the router's "not in the schema" error.

Not in the game's DDL - this is a Hermes protocol - so the layout was read out of the client:
request stubs 8DFEF0, 8E0110, 8E0210 and 8E0300, replies parsed by 5A4C10 (the key, 8 bytes)
and 8E0A00 (a u32 count, then {u32 key, u32 value} pairs, 91B370).

GetMatchUniqueKey gates hosting. The lobby-host step waits for its answer with no timeout, gives
up on an error, and on success stores the key in the session blob every peer gets. The key is
then the match's identity: each game compares it with the last one it saw to decide that a new
match started (and sends FPSCLIENT_START), and prints it with %llu as the MATCHID of every
telemetry tag. So it is non-zero and never repeats - a counter that starts from the clock, and
from past the last key handed out when that is later, so that a restart with a clock set back
cannot repeat one either.

GetGenericSettings is a list of {key, value} the client looks up by key, the last entry winning
and a missing key reading as 0 = off. What the client does with them:

    100   how many players may stream at once on one channel of a session - voice, most likely.
          The client's default is 3, clamped to 16; read only when the session manager is built.
    2     an XP multiplier x 10 (20 = double XP), applied only between the dates in 0 and 1
    0, 1  the start and end of that window, as packed dates
    4, 5  a daily bonus: 4 is how many matches a day get it, 5 the multiplier x 10
    3     a challenge reset id (0 = none)
    6     anything above 0 makes the profile manager grant item 1150 (unverified)

The default answer is 100 = 3, which is what the client would do without it. The config
setting generic_settings adds to it or overrides it, e.g.

    generic_settings = 2=20, 0=2026-12-24, 1=2026-12-27 06:00

A value is a number (0x.. allowed) or a date YYYY-MM-DD[ HH:MM], packed the way the client
unpacks it (A68C40): year - 1000 in bits 0..10, month 11..14, day 15..19, hour 20..24, minute
25..30, on the client's own clock. A malformed entry is skipped with a warning.

UnlockPrivilegeTrial answers success and nothing else. PrivilegesProtocol already lists the four
privileges the client asks about at sign-in as owned for good, so a game that read that list
never asks; one that asks anyway is told it worked.
"""

import datetime
import json
import os
import re
import time

from .. import protocols
from ..rmc.router import Service

DEFAULT_SETTINGS = {100: 3}
U32 = 0xFFFFFFFF


def pack_date(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> int:
    """A date as the client's GetGenericSettings reader unpacks it. A day the calendar does not
    have (2026-02-30) is refused here: the client would quietly roll it into the next month."""
    if not 1000 <= year < 1000 + 0x800:
        raise ValueError("year out of range")
    datetime.datetime(year, month, day, hour, minute)       # ValueError for a date that is not
    return (year - 1000) | month << 11 | day << 15 | hour << 20 | minute << 25


def parse_settings(text: str, warn=None) -> dict:
    """'2=20, 0=2026-12-24 18:00' -> {2: 20, 0: packed}. Bad entries go to `warn` and are
    left out."""
    out = {}
    for entry in (text or "").split(","):
        entry = entry.strip()
        if not entry:
            continue
        try:
            key, value = (part.strip() for part in entry.split("=", 1))
            key = int(key, 0)
            date = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})(?:[ T](\d{1,2}):(\d{2}))?", value)
            if date:
                value = pack_date(*(int(g) for g in date.groups() if g is not None))
            else:
                value = int(value, 0)
            if not (0 <= key <= U32 and 0 <= value <= U32):
                raise ValueError("out of range")
        except ValueError as e:
            if warn:
                warn("generic_settings: ignoring %r (%s)", entry, e)
            continue
        out[key] = value
    return out


class MatchKeys:
    """Where the match keys come from, and the last one handed out, kept across restarts."""

    def __init__(self, path: str = None, log=None):
        self.path = path
        self.log = log
        self.last = 0
        self.load()

    def load(self) -> None:
        if not self.path or not os.path.exists(self.path):
            return
        try:
            with open(self.path, encoding="utf-8") as f:
                self.last = int(json.load(f).get("last", 0))
        except (OSError, ValueError, AttributeError) as e:
            if self.log:
                self.log.error("cannot read %s (%s) - match keys start from the clock",
                               self.path, e)

    def save(self) -> None:
        if not self.path:
            return
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"last": self.last}, f)
            os.replace(tmp, self.path)
        except OSError as e:
            if self.log:
                self.log.error("cannot write %s: %s", self.path, e)

    def next(self) -> int:
        # Milliseconds since 1970 with 12 bits to spare: 4096 matches a millisecond before the
        # counter runs ahead of the clock, and still far inside a signed 64-bit number.
        self.last = max(self.last + 1, int(time.time() * 1000) << 12)
        return self.last


class Misc(Service):
    protocol_id = protocols.MISC
    protocol_name = "MiscProtocol"

    schema_classes = [
        ("MiscGenericSetting", [("key", "uint32"), ("value", "uint32")]),
    ]
    schema_methods = {
        1: ("GetMatchUniqueKey", [("out", "uint64", "uniqueKey")], None),
        3: ("GetGenericSettings", [("out", "qlist<MiscGenericSetting>", "settings")], None),
        4: ("UnlockPrivilegeTrial", [("in", "uint32", "privilegeId")], None),
        5: ("UnlockPrivilegeTrial2", [("in", "uint32", "privilegeId")], None),
    }

    def __init__(self, backend):
        super().__init__(backend)
        self.keys = backend.add_store(MatchKeys(backend.data_path("match_keys.json"), self.log))
        self.settings = dict(DEFAULT_SETTINGS)
        self.settings.update(parse_settings(backend.config.get("generic_settings"),
                                            self.log.warning))

    def GetMatchUniqueKey(self, ctx):
        key = self.keys.next()
        self.log.info("%s hosts match %d", ctx.client.name or ctx.client.address, key)
        return {"uniqueKey": key}

    def GetGenericSettings(self, ctx):
        return {"settings": [{"key": k, "value": v} for k, v in sorted(self.settings.items())]}

    def UnlockPrivilegeTrial(self, ctx, privilegeId):
        self.log.info("%s unlocks a trial of privilege %d", ctx.client.name or ctx.client.address,
                      privilegeId)
        return {}

    def UnlockPrivilegeTrial2(self, ctx, privilegeId):
        return self.UnlockPrivilegeTrial(ctx, privilegeId)
