"""Accounts: the register of everyone who has ever signed in, and their permanent pid.

A player's pid is their identity everywhere else - in a session, in someone's friends list, in an
invitation - so it has to survive restarts and must not be reused. Removing a friend in the
previous attempt deleted the whole record including the pid, which made that player unfindable
and so impossible to add back; the register and the relationships between its entries are kept
apart here for that reason.

Stored as a small JSON file. Account *names* are in it; passwords never are, because the game's
credentials are not read at all (see rmc.ddl.SENSITIVE).
"""

import json
import os
import time

from . import log as logs
from .kerberos import SERVER_PID, TRACKING_NAME, TRACKING_PID

# The pid the first real player gets. Everything below is reserved: 0x1000 is the server itself,
# 0x1111 the telemetry user. Starting here also means a fresh install hands out the same pid the
# earlier verified runs used, which keeps old captures comparable.
FIRST_PID = 0x1234
RESERVED = {SERVER_PID, TRACKING_PID}


class Account:
    __slots__ = ("name", "pid", "first_seen", "last_seen")

    def __init__(self, name: str, pid: int, first_seen: float = None, last_seen: float = None):
        self.name = name
        self.pid = pid
        self.first_seen = first_seen or time.time()
        self.last_seen = last_seen or self.first_seen

    def as_dict(self) -> dict:
        return {"name": self.name, "pid": self.pid, "first_seen": self.first_seen,
                "last_seen": self.last_seen}


class Accounts:
    def __init__(self, path: str = None):
        self.path = path
        self.log = logs.get("accounts")
        self.by_name = {}
        self.by_pid = {}
        self._next_pid = FIRST_PID
        self.load()

    # ----- storage --------------------------------------------------------------------------
    def load(self) -> None:
        if not self.path or not os.path.exists(self.path):
            return
        try:
            with open(self.path, encoding="utf-8") as f:
                rows = json.load(f)
        except (OSError, ValueError) as e:
            self.log.error("cannot read %s (%s) - starting with an empty register", self.path, e)
            return
        for row in rows:
            account = Account(row["name"], int(row["pid"]), row.get("first_seen"),
                              row.get("last_seen"))
            self.by_name[account.name.lower()] = account
            self.by_pid[account.pid] = account
            self._next_pid = max(self._next_pid, account.pid + 1)
        self.log.info("%d account(s) from %s", len(self.by_name), self.path)

    def save(self) -> None:
        if not self.path:
            return
        rows = [a.as_dict() for a in sorted(self.by_name.values(), key=lambda a: a.pid)]
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(rows, f, indent=1)
            os.replace(tmp, self.path)
        except OSError as e:
            self.log.error("cannot write %s: %s", self.path, e)

    # ----- lookup ---------------------------------------------------------------------------
    def get(self, name: str):
        return self.by_name.get((name or "").lower())

    def pid_for(self, name: str) -> int:
        """The pid of this account, giving it one the first time it is seen."""
        if name == TRACKING_NAME:
            return TRACKING_PID
        account = self.get(name)
        if account is None:
            pid = self._allocate()
            account = Account(name, pid)
            self.by_name[name.lower()] = account
            self.by_pid[pid] = account
            self.log.info("new account %s, pid 0x%X", name, pid)
            self.save()
        else:
            account.last_seen = time.time()
        return account.pid

    def name_for(self, pid: int) -> str:
        if pid == TRACKING_PID:
            return TRACKING_NAME
        account = self.by_pid.get(pid)
        return account.name if account else ""

    def _allocate(self) -> int:
        pid = self._next_pid
        while pid in RESERVED or pid in self.by_pid:
            pid += 1
        self._next_pid = pid + 1
        return pid

    def __len__(self):
        return len(self.by_name)
