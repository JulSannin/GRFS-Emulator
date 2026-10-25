"""UplayWinProtocol (0x78) and GR5AwardProtocol (0x77): Uplay's actions and rewards, and the
game's own awards.

Neither is in the game's DDL - both are Ubisoft's own - so the layouts here were read out of the
client: the request stubs C950F0 (1), C94F30 (5), C94DB0 (7), C94C20 (8), C94A90 (9) and the
reply dispatcher C94810 for Uplay; stubs 8E21B0, 8E22C0, 8E23E0 and dispatcher 8E2BB0 for the
awards. The binary has no method names for Uplay; the ones below are what the same protocol is
called in other Ubisoft games of those years, and every layout was checked against this client.

Uplay
-----
After sign-in the Uplay layer (ONL_cl_Uplay, state machine A45580) runs a fixed sequence, and
moves on to the next call once the previous one is no longer pending, whatever it returned:

    7 UplayWelcome            (locale, platform)
    9 SetActionsCompleted     (codes queued while offline, locale, platform)
    5 GetRewards              (0, 100, "GRFS", locale, platform)
    1 GetActions              (0, 100, "GRFS", locale, platform)  - only if 5 succeeded

The state machine is polled every frame and waits for as long as a call is pending, so a call
left unanswered stops the sequence at that step for the rest of the session. Every one of them
gets an answer. An error is as good as an answer for 7 and 9; one for 5 skips 1 and the rewards.

What the answers do:

* GetRewards is the one that matters. A44DD0 walks the rewards, keeps those with a platform
  entry for "PC" that is marked purchased, and for each code it knows - GRFSREWARD01PC, 02, 03,
  04 - marks the reward as owned and unlocks a privilege for it through Misc
  UnlockPrivilegeTrial (0x1432, 0x143C, 0x1446, 0x1450), unless the player already has that
  privilege. All four are answered as purchased: this server hands out every privilege anyway
  (PrivilegesProtocol lists those four too), so the rewards are simply there, as they would be
  for a player who had bought them with Uplay units.
* SetActionCompleted (8) is the game telling Uplay an action was done - actions "1" to "4",
  worth 10, 20, 30 and 40 units (A66460). The reply's value and name are copied back (A44100)
  and the completion callback (A663F0) posts the "unlocked" message; a value of -1 sets the
  client's "did not happen" flag instead, so nothing is shown. That is what an action code nobody
  knows gets.
* GetActions' answer is walked and dropped (A442A0), UplayWelcome's and SetActionsCompleted's
  only end the wait. They carry the real catalogue anyway, with what this player completed.

Completed actions are kept per pid in uplay.json.

GR5Award
--------
The award/medal system of the game script. grantAwards (3) sends the ids of awards just earned;
getGrantedAwards (2) reads them back, for a pid and a range (the client asks for 0..150). Each
entry is {award id, u32}: the task keeps only the first (8D0820), and what the second means is
not known, so it is 0. getGrantedAwardsCount (1) has no caller in the game; it is answered
anyway, since its layout is known. Granted awards are kept per pid in awards.json.

The pid in getGrantedAwards is a u32 from the client's per-pad record - the same one it sends,
as text, to TrackingExtension UserIsVIP - and is most probably the player's pid. If it is not a
pid this server ever handed out, the caller's own awards are answered.
"""

import json
import os

from .. import protocols
from ..kerberos import TRACKING_PID
from ..rmc.router import Errors, RmcError, Service

# Uplay's codes for the platform the client asks about; this is the PC game.
PLATFORM = "PC"
GAME_CODE = "GRFS"

# What the client reads as "not completed" in an action's value.
NOT_DONE = 0xFFFFFFFF

# The actions the game unlocks (A66460: index 0..3 -> code "1".."4", value 10..40).
ACTIONS = [(str(n), "Uplay action %d" % n, "", 10 * n) for n in range(1, 5)]

# The rewards the client looks for in GetRewards, with their cost in units. The PS3 and Xbox 360
# variants of the first exist in the client's list too, but are only taken on those platforms.
REWARDS = [
    ("GRFSREWARD01PC", "Uplay reward 1", 10),
    ("GRFSREWARD02", "Uplay reward 2", 20),
    ("GRFSREWARD03", "Uplay reward 3", 30),
    ("GRFSREWARD04", "Uplay reward 4", 40),
]


class _PerPid:
    """pid -> a list of values, in a JSON file written whole (temp file, then replace)."""

    def __init__(self, path: str, what: str, log):
        self.path = path
        self.what = what
        self.log = log
        self.rows = {}
        self.load()

    def load(self) -> None:
        if not self.path or not os.path.exists(self.path):
            return
        try:
            with open(self.path, encoding="utf-8") as f:
                rows = json.load(f)
            self.rows = {int(pid): list(values) for pid, values in rows.items()}
        except (OSError, ValueError, TypeError, AttributeError) as e:
            self.log.error("cannot read %s (%s) - starting with no %s", self.path, e, self.what)
            self.rows = {}
            return
        self.log.info("%s for %d player(s) from %s", self.what, len(self.rows), self.path)

    def save(self) -> None:
        if not self.path:
            return
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({str(pid): values for pid, values in sorted(self.rows.items())}, f,
                          indent=1)
            os.replace(tmp, self.path)
        except OSError as e:
            self.log.error("cannot write %s: %s", self.path, e)

    def get(self, pid: int) -> list:
        return list(self.rows.get(pid, ()))

    def add(self, pid: int, values) -> list:
        """Append what is not there yet; returns what was new. A value repeated within one call
        counts once. Saved at once - these change a few times a match, and a crash should not
        lose them."""
        have = self.rows.setdefault(pid, [])
        new = []
        for v in values:
            if v not in have and v not in new:
                new.append(v)
        if new:
            have.extend(new)
            self.save()
        return new


def _pid_of(ctx) -> int:
    if not ctx.client.pid:
        raise RmcError(Errors.RV_NOT_AUTHENTICATED, "before logging in")
    return ctx.client.pid


class UplayWin(Service):
    protocol_id = protocols.UPLAY_WIN
    protocol_name = "UplayWinProtocol"

    schema_classes = [
        ("UPlayActionPlatform", [("m_platformCode", "string"), ("m_completed", "bool"),
                                 ("m_specificKey", "string")]),
        ("UPlayAction", [("m_code", "string"), ("m_name", "string"), ("m_description", "string"),
                         ("m_value", "uint32"), ("m_gameCode", "string"),
                         ("m_platforms", "std_list<UPlayActionPlatform>")]),
        ("UPlayRewardPlatform", [("m_platformCode", "string"), ("m_purchased", "bool")]),
        ("UPlayReward", [("m_code", "string"), ("m_name", "string"), ("m_description", "string"),
                         ("m_value", "uint32"), ("m_rewardTypeName", "string"),
                         ("m_gameCode", "string"),
                         ("m_platforms", "std_list<UPlayRewardPlatform>")]),
    ]
    schema_methods = {
        1: ("GetActions", [("in", "uint32", "startRowIndex"), ("in", "uint32", "maximumRows"),
                           ("in", "string", "sortExpression"), ("in", "string", "cultureName"),
                           ("in", "string", "platformCode"),
                           ("out", "std_list<UPlayAction>", "actions")], None),
        5: ("GetRewards", [("in", "uint32", "startRowIndex"), ("in", "uint32", "maximumRows"),
                           ("in", "string", "sortExpression"), ("in", "string", "cultureName"),
                           ("in", "string", "platformCode"),
                           ("out", "std_list<UPlayReward>", "rewards")], None),
        7: ("UplayWelcome", [("in", "string", "cultureName"), ("in", "string", "platformCode"),
                             ("out", "std_list<UPlayAction>", "actions")], None),
        8: ("SetActionCompleted", [("in", "string", "actionCode"),
                                   ("in", "string", "cultureName"),
                                   ("in", "string", "platformCode"),
                                   ("out", "UPlayAction", "action")], None),
        9: ("SetActionsCompleted", [("in", "std_list<string>", "actionCodeList"),
                                    ("in", "string", "cultureName"),
                                    ("in", "string", "platformCode"),
                                    ("out", "std_list<UPlayAction>", "actions")], None),
    }

    def __init__(self, backend):
        super().__init__(backend)
        self.completed = backend.add_store(_PerPid(backend.data_path("uplay.json"),
                                                   "completed Uplay actions", self.log))

    # ----- the sequence after sign-in -----------------------------------------------------------
    def UplayWelcome(self, ctx, cultureName="", platformCode=PLATFORM):
        """Only ends the wait; nothing reads it. The actions this player has done."""
        done = set(self.completed.get(_pid_of(ctx)))
        return {"actions": [a for a in self._actions(done) if a["m_code"] in done]}

    def SetActionsCompleted(self, ctx, actionCodeList, cultureName="", platformCode=PLATFORM):
        """Actions done while Uplay could not be told. Usually none."""
        pid = _pid_of(ctx)
        known = [code for code in actionCodeList or [] if self._action(code)]
        self._complete(ctx, pid, known)
        done = set(self.completed.get(pid))
        return {"actions": [a for a in self._actions(done) if a["m_code"] in known]}

    def GetRewards(self, ctx, startRowIndex=0, maximumRows=0, sortExpression="",
                   cultureName="", platformCode=PLATFORM):
        rewards = [{"m_code": code, "m_name": name, "m_description": "", "m_value": value,
                    "m_rewardTypeName": "", "m_gameCode": GAME_CODE,
                    "m_platforms": [{"m_platformCode": PLATFORM, "m_purchased": True}]}
                   for code, name, value in REWARDS]
        return {"rewards": _page(rewards, startRowIndex, maximumRows)}

    def GetActions(self, ctx, startRowIndex=0, maximumRows=0, sortExpression="",
                   cultureName="", platformCode=PLATFORM):
        done = set(self.completed.get(_pid_of(ctx)))
        return {"actions": _page(self._actions(done), startRowIndex, maximumRows)}

    # ----- in play ------------------------------------------------------------------------------
    def SetActionCompleted(self, ctx, actionCode, cultureName="", platformCode=PLATFORM):
        pid = _pid_of(ctx)
        if not self._action(actionCode):
            self.log.info("%s completed Uplay action %r, which does not exist", ctx.client.name,
                          actionCode)
            return {"action": {"m_code": actionCode or "", "m_name": "", "m_description": "",
                               "m_value": NOT_DONE, "m_gameCode": GAME_CODE,
                               "m_platforms": []}}
        self._complete(ctx, pid, [actionCode])
        return {"action": self._actions({actionCode}, only=actionCode)[0]}

    # --------------------------------------------------------------------------------------------
    @staticmethod
    def _action(code):
        return next((a for a in ACTIONS if a[0] == code), None)

    @staticmethod
    def _actions(done: set, only: str = None) -> list:
        return [{"m_code": code, "m_name": name, "m_description": text, "m_value": value,
                 "m_gameCode": GAME_CODE,
                 "m_platforms": [{"m_platformCode": PLATFORM, "m_completed": code in done,
                                  "m_specificKey": ""}]}
                for code, name, text, value in ACTIONS if only is None or code == only]

    def _complete(self, ctx, pid: int, codes: list) -> None:
        new = self.completed.add(pid, codes)
        if new:
            self.log.info("%s completed Uplay action(s) %s", ctx.client.name, ", ".join(new))


def _page(rows: list, offset: int, size: int) -> list:
    offset = offset or 0
    return rows[offset:offset + size] if size else rows[offset:]


class GR5Award(Service):
    protocol_id = protocols.GR5_AWARD
    protocol_name = "GR5AwardProtocol"

    schema_classes = [
        ("GR5GrantedAward", [("m_awardId", "uint32"), ("m_param", "uint32")]),
    ]
    schema_methods = {
        1: ("getGrantedAwardsCount", [("in", "uint32", "pid"), ("out", "uint32", "count")], None),
        2: ("getGrantedAwards", [("in", "uint32", "pid"), ("in", "ResultRange", "resultRange"),
                                 ("out", "std_list<GR5GrantedAward>", "awards")], None),
        3: ("grantAwards", [("in", "std_list<uint32>", "awardIds")], None),
    }

    def __init__(self, backend):
        super().__init__(backend)
        self.granted = backend.add_store(_PerPid(backend.data_path("awards.json"),
                                                 "granted awards", self.log))

    def getGrantedAwardsCount(self, ctx, pid=0):
        return {"count": len(self.granted.get(self._whose(ctx, pid)))}

    def getGrantedAwards(self, ctx, pid=0, resultRange=None):
        awards = self.granted.get(self._whose(ctx, pid))
        offset, size = 0, 0
        if isinstance(resultRange, dict):
            offset = resultRange.get("m_uiOffset", 0) or 0
            size = resultRange.get("m_uiSize", 0) or 0
        return {"awards": [{"m_awardId": a, "m_param": 0} for a in _page(awards, offset, size)]}

    def grantAwards(self, ctx, awardIds):
        pid = _pid_of(ctx)
        sent = awardIds or []
        new = self.granted.add(pid, sent)
        had = len(set(sent)) - len(new)
        self.log.info("%s earned award(s) %s%s", ctx.client.name,
                      ", ".join(str(a) for a in new) or "-",
                      " (%d already granted)" % had if had else "")
        return {}

    def _whose(self, ctx, pid: int) -> int:
        """The pid asked about, if it is a player this server knows; otherwise the caller. The
        telemetry user has a name but is nobody's record."""
        if pid and pid != TRACKING_PID and (self.backend.accounts.name_for(pid)
                                            or pid == ctx.client.pid):
            return pid
        own = _pid_of(ctx)
        if pid:
            self.log.debug("awards asked for 0x%X, which is nobody here - answering for 0x%X",
                           pid, own)
        return own
