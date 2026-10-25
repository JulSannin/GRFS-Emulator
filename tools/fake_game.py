"""A second player you drive by hand: one real game on one side, this on the other.

    python tools/fake_game.py --name bob                     # server on this machine
    python tools/fake_game.py --name bob --host 192.168.1.8

It signs in exactly as the game does (grfs.fakegame, the client the tests use), stays online by
pinging the way the game does, prints every call the server makes to it as it arrives - a friends
change or a game invitation in words - and answers them. The friends commands send what the
game's friends screen sends: `invites` is GetRelationships' rel 3, `blocked` is
GetDetailedList(3), and `unblock` is a friend request by name, as the BLOCKLIST tab's Unblock is.
`sessions`, `clan`, `stats` and `maps` make the calls of the squad lobby, the clan screen, the
profile manager and a matchmade host, so that a real game on the other side has somebody to
invite it, to be invited, to share a clan with. Type `help` for the commands.

What it cannot do is be reached: it has no peer-to-peer side, so a real game that tries to join
a session hosted here gets no answer from it.
"""

import argparse
import cmd
import json
import os
import queue
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from grfs import clans as clan_model                                          # noqa: E402
from grfs import friends as model                                             # noqa: E402
from grfs import maps as map_model                                            # noqa: E402
from grfs import sessions as session_model                                    # noqa: E402
from grfs import stats as stat_model                                          # noqa: E402
from grfs.fakegame import Peer, Player, RmcFailure, fetch_config, url_address  # noqa: E402
from grfs.services.game_session import INVITATION_ACCEPTED, INVITATION_RECEIVED  # noqa: E402
from grfs.services.overlord import DARE_RECEIVED                             # noqa: E402

KEEP_ONLINE_EVERY = 10.0       # seconds; the server counts a player as away after 90

G = "GameSessionProtocol"
SESSION_TYPE = 1               # the squad lobby and the co-op match alike
# What a hand-made session carries besides its capacities and counts: two of the game's own
# attributes, near enough to a custom match.
GAME_ATTRIBUTES = {0x64: 1, 0x65: 2}
# The range the game always asks its invitations with.
EVERYTHING = {"m_uiOffset": 0, "m_uiSize": 25}
ROLES = {clan_model.LEADER: "leader", clan_model.NEWCOMER: "newcomer"}     # 1 and 2 have no names


class FakeGame(cmd.Cmd):
    prompt = "> "

    def __init__(self, player: Player):
        super().__init__()
        self.player = player
        self.session = None        # the key of the session this game is in, if any
        self.hosting = False
        self.prompt = "%s> " % player.name
        self.stop = threading.Event()
        threading.Thread(target=self._keep_online, daemon=True).start()

    # ----- around every command -------------------------------------------------------------
    def precmd(self, line):
        self._show_server_calls()
        return line

    def postcmd(self, stop, line):
        self._show_server_calls()
        return stop

    def onecmd(self, line):
        try:
            return super().onecmd(line)
        except RmcFailure as e:
            print("refused: %s" % e)
        except (TimeoutError, ValueError, LookupError) as e:
            print("error: %s" % e)
        return False

    def emptyline(self):
        return False

    def _keep_online(self):
        while not self.stop.wait(KEEP_ONLINE_EVERY):
            try:
                self.player.secure.ping()
            except Exception as e:
                print("\n(ping failed: %s)" % e)

    def _show_server_calls(self):
        while True:
            try:
                protocol, method, args = self.player.next_server_call(timeout=0)
            except queue.Empty:
                return
            print("<< %s" % self._describe(protocol, method, args))

    def _describe(self, protocol, method, args) -> str:
        """The notifications the game acts on in words, anything else as it came."""
        event = args.get("oEvent") if isinstance(args, dict) else None
        if protocol == "NotificationProtocol" and event:
            kind = event["m_uiType"]
            if kind // 1000 == 1:
                what = ("sent a friend request" if event["m_uiParam2"] == model.NEW_REQUEST
                        else "changed: came, went, accepted, removed or blocked")
                return "friends: %s %s - the game would refetch its list now" % (
                    self._name_of(event["m_uiParam1"]), what)
            who = self._name_of(event["m_pidSource"])
            if kind == INVITATION_RECEIVED:
                return ("invitation: %s asks you into session %d: %r - `sessions invitations`"
                        " shows it, `sessions accept-invite` takes it"
                        % (who, event["m_uiParam2"], event["m_strParam"]))
            if kind == INVITATION_ACCEPTED:
                return "invitation: %s accepted yours to session %d" % (who, event["m_uiParam2"])
            if kind == DARE_RECEIVED:
                return "dare: %s dares you (dare %d, score %d)" % (
                    who, event["m_uiParam2"], event["m_uiParam3"])
        return "the server called %s.%s %s" % (protocol, method, json.dumps(args, default=str))

    def _name_of(self, pid: int) -> str:
        try:
            names = dict(self.player.call("UbiAccountManagementProtocol",
                                          "LookupUbiAccountIDsByPids", {"pids": [pid]})
                         ["ubiaccountIDs"])
        except Exception:
            names = {}
        return names.get(pid) or "pid 0x%X" % pid

    def pid_of(self, name: str) -> int:
        pids = dict(self.player.call("UbiAccountManagementProtocol", "LookupPrincipalIds",
                                     {"ubiAccountIds": [name]})["pids"])
        if not pids:
            raise KeyError("nobody called %s has signed in here" % name)
        return next(iter(pids.values()))

    # ----- commands -------------------------------------------------------------------------
    def do_whoami(self, _):
        """whoami - name, pid, connection id and where this game is registered"""
        p = self.player
        print("%s pid 0x%X, connection %d, socket %s:%d" % (p.name, p.pid, p.rvcid,
                                                            *p.wire.address))
        print("registered: %s" % p.register_reply["urlPublic"])

    def do_search(self, text):
        """search TEXT - FIND FRIENDS"""
        for row in self.player.call("SearchFriendsProtocol", "SearchFriends",
                                    {"partOfName": text})["foundList"]:
            print("  %-20s pid 0x%X" % (row["m_strName"], row["m_pid"]))

    def do_add(self, line):
        """add NAME [MESSAGE] - send a friend request"""
        name, _, text = line.partition(" ")
        print(self.player.call("FriendsProtocol", "AddFriendByName", {
            "strPlayerName": name, "uiDetails": 1, "strMessage": text})["retval"])

    def _by_pid(self, method, name):
        print(self.player.call("FriendsProtocol", method,
                               {"uiPlayer": self.pid_of(name)})["retval"])

    def do_accept(self, name):
        """accept NAME - accept their request"""
        self._by_pid("AcceptFriendship", name)

    def do_decline(self, name):
        """decline NAME - decline their request"""
        self._by_pid("DeclineFriendship", name)

    def do_remove(self, name):
        """remove NAME - Remove Friend"""
        self._by_pid("ClearRelationship", name)

    def do_block(self, name):
        """block NAME - Block"""
        self._by_pid("BlackList", name)

    def do_unblock(self, name):
        """unblock NAME - BLOCKLIST's Unblock, which is a friend request by name and nothing else"""
        print(self.player.call("FriendsProtocol", "AddFriendByName", {
            "strPlayerName": name, "uiDetails": 0, "strMessage": ""})["retval"])

    def do_blocked(self, _):
        """blocked - the BLOCKLIST tab: GetDetailedList asked about 3"""
        rows = self.player.call("FriendsProtocol", "GetDetailedList",
                                {"byRelationship": 3, "bReversed": False})["lstFriendsList"]
        if not rows:
            print("  nobody")
        for row in rows:
            print("  %-20s pid 0x%X" % (row["m_strName"], row["m_pid"]))

    def do_list(self, _):
        """list - the contact list, as GetRelationships returns it"""
        rows = self.player.relationships()
        if not rows:
            print("  nobody")
        for name, (relationship, online) in sorted(rows.items()):
            print("  %-20s %-17s %s" % (name, model.NAMES.get(relationship, relationship),
                                        "online" if online else "offline"))

    def do_invites(self, _):
        """invites - INVITATIONS: GetRelationships' requests received, and the text that came
        with them"""
        answer = self.player.call("FriendsProtocol", "GetRelationships",
                                  {"resultRange": {"m_uiOffset": 0, "m_uiSize": 200}})
        rows = [r for r in answer["lstRelationshipsList"]
                if r["m_byRelationship"] == model.REQUEST_RECEIVED]
        texts = {m["m_senderPid"]: m["m_message"] for m in self.player.call(
            "FriendMessagesExtensionServiceProtocol", "ReceiveFriendInvitationMessages")["messages"]}
        if not rows:
            print("  none")
        for row in rows:
            print("  %-20s %-8s %s" % (row["m_strName"],
                                       "online" if row["m_byStatus"] else "offline",
                                       texts.get(row["m_pid"], "")))

    def do_uplay(self, _):
        """uplay - UbiFriends' list, merged into the same screen, and the names it resolves to"""
        rows = self.player.call("UbiFriendsProtocol", "GetRelationshipsList")["relationshipsList"]
        ids = [r["m_to"]["m_ubiAccountId"] for r in rows]
        names = dict(self.player.call("UbiAccountManagementProtocol",
                                      "LookupUsernamesByUbiAccountIDs",
                                      {"UbiAccountIds": ids})["Usernames"]) if ids else {}
        if not rows:
            print("  nobody")
        for r in rows:
            account = r["m_to"]["m_ubiAccountId"]
            print("  %-20s %-17s%s" % (names.get(account, "?" + account),
                                       model.NAMES.get(r["m_relationship"], r["m_relationship"]),
                                       " blocked" if r["m_onBlacklist"] else ""))

    def do_urls(self, name):
        """urls NAME - where the server says another player can be reached"""
        print(self.player.call("SecureConnectionProtocol", "RequestURLs",
                               {"cidTarget": 0, "pidTarget": self.pid_of(name)}))

    def do_probe(self, _):
        """probe - the NAT echo, from a socket of its own as the game's P2P socket does"""
        host = self.player.host
        with Peer(bind="127.0.0.1" if host.startswith("127.") else "0.0.0.0") as peer:
            print(peer.probe(host, self.player.secure_port) or "no answer")

    # ----- the squad lobby and co-op: GameSessionProtocol -----------------------------------
    def do_sessions(self, line):
        """sessions - the session this game is in
        sessions host [PUBLIC] [PRIVATE] - create one (4 public and 0 private slots by default;
                                           0 public is a squad, which no search finds) and add
                                           yourself, as a host does once its lobby is up
        sessions search [QUERY] - what a search finds: 2 the browser (default), 3 quick match
        sessions join ID - look the session up and send JoinSession; the host adds you, not this
        sessions invite NAME [MESSAGE] - invite somebody into your session
        sessions invitations - the invitations you have, the newest last, as the game shows them
        sessions accept-invite [N] - accept one, the newest by default, as the game does
        sessions leave - leave your session, or abandon it if you host it"""
        words = line.split()
        what, rest = (words[0], words[1:]) if words else ("", [])
        handler = getattr(self, "_session_" + what.replace("-", "_"), None)
        if handler is None:
            print("sessions [host|search|join|invite|invitations|accept-invite|leave]")
            return
        handler(rest)

    def _session_(self, _):
        if self.session is None:
            print("  in no session")
            return
        self._show_session(self._get_session(self.session["m_sessionID"]))

    def _session_host(self, rest):
        public = int(rest[0]) if rest else 4
        private = int(rest[1]) if len(rest) > 1 else 0
        values = {session_model.PUBLIC_SLOTS: public, session_model.PRIVATE_SLOTS: private,
                  session_model.USED_PUBLIC: 0, session_model.USED_PRIVATE: 0,
                  session_model.INVITE_ONLY: 0 if public else 1, **GAME_ATTRIBUTES}
        key = self.player.call(G, "CreateSession", {"gameSession": {
            "m_typeID": SESSION_TYPE, "m_attributes": _props(values)}})["gameSessionKey"]
        slot = "publicParticipantIDs" if public else "privateParticipantIDs"
        self.player.call(G, "AddParticipants", {"gameSessionKey": key, slot: [self.player.pid]})
        self.session, self.hosting = key, True
        print("hosting session %d (%d public, %d private)" % (key["m_sessionID"], public, private))

    def _session_search(self, rest):
        query = int(rest[0]) if rest else session_model.QUERY_BROWSE
        found = self.player.call(G, "SearchSessions", {"gameSessionQuery": {
            "m_typeID": SESSION_TYPE, "m_queryID": query, "m_parameters": []}})["searchResults"]
        if not found:
            print("  nothing")
        for result in found:
            self._show_session(result)

    def _session_join(self, rest):
        if len(rest) != 1:
            print("sessions join ID")
            return
        key = {"m_typeID": SESSION_TYPE, "m_sessionID": int(rest[0])}
        self._show_session(self._get_session(key["m_sessionID"]))
        self.player.call(G, "JoinSession", {"gameSessionKey": key})
        self.session, self.hosting = key, False
        print("joined as far as the server goes: the host's AddParticipants puts you in")

    def _session_invite(self, rest):
        if not rest or self.session is None:
            print("sessions invite NAME [MESSAGE], from inside a session")
            return
        self.player.call(G, "SendInvitation", {"invitation": {
            "m_sessionKey": self.session, "m_recipientPIDs": [self.pid_of(rest[0])],
            "m_message": " ".join(rest[1:]) or "Do you want to play?"}})
        print("invited %s to session %d" % (rest[0], self.session["m_sessionID"]))

    def _invitations(self) -> list:
        return self.player.call(G, "GetInvitationsReceived", {
            "gameSessionTypeID": SESSION_TYPE, "resultRange": EVERYTHING})["invitations"]

    def _session_invitations(self, _):
        invitations = self._invitations()
        if not invitations:
            print("  none")
        for n, i in enumerate(invitations, 1):
            print("  %d. %-20s session %-4d %s" % (n, self._name_of(i["m_senderPID"]),
                                                   i["m_sessionKey"]["m_sessionID"],
                                                   i["m_message"]))

    def _session_accept_invite(self, rest):
        invitations = self._invitations()
        if not invitations:
            print("no invitation to accept")
            return
        invitation = invitations[int(rest[0]) - 1 if rest else -1]
        # As the game does it: look the session up, reach its host, then accept.
        self._show_session(self._get_session(invitation["m_sessionKey"]["m_sessionID"]))
        self.player.call(G, "AcceptInvitation", {"gameSessionInvitation": invitation})
        self.session, self.hosting = invitation["m_sessionKey"], False
        print("accepted %s's invitation: the host's AddParticipants puts you in"
              % self._name_of(invitation["m_senderPID"]))

    def _session_leave(self, _):
        if self.session is None:
            print("in no session")
            return
        self.player.call(G, "AbandonSession" if self.hosting else "LeaveSession",
                         {"gameSessionKey": self.session})
        print("%s session %d" % ("abandoned" if self.hosting else "left",
                                 self.session["m_sessionID"]))
        self.session, self.hosting = None, False

    def _get_session(self, session_id: int) -> dict:
        return self.player.call(G, "GetSession", {"gameSessionKey": {
            "m_typeID": SESSION_TYPE, "m_sessionID": session_id}})["searchResult"]

    def _show_session(self, result: dict):
        values = {a["m_ID"]: a["m_value"] for a in result["m_attributes"]}
        print("  session %-4d host %-20s public %d/%d private %d/%d%s" % (
            result["m_sessionKey"]["m_sessionID"], self._name_of(result["m_hostPID"]),
            values.get(session_model.USED_PUBLIC, 0), values.get(session_model.PUBLIC_SLOTS, 0),
            values.get(session_model.USED_PRIVATE, 0), values.get(session_model.PRIVATE_SLOTS, 0),
            " squad" if values.get(session_model.INVITE_ONLY) == 1 else ""))
        for url in result["m_hostURLs"]:
            print("    %s" % url)

    # ----- the clan screen: ClansProtocol ---------------------------------------------------
    def do_clan(self, line):
        """clan - the clan you are in
        clan create NAME TAG [DESCRIPTION] - found one (a tag of up to three letters)
        clan invite NAME - invite somebody into yours
        clan join [ID] - join a clan that invited you, the first one by default
        clan members [ID] - who is in a clan, yours by default
        clan leave - leave yours; the last one out ends it"""
        words = line.split()
        what = words[0] if words else ""
        if what == "create" and len(words) >= 3:
            answer = self.player.call("ClansProtocol", "CreateClan", {
                "name": words[1], "tag": words[2], "description": " ".join(words[3:])})
            self._clan_result(answer["result"], "founded clan %d" % answer["clanId"])
        elif what == "invite" and len(words) == 2:
            self._clan_result(self.player.call("ClansProtocol", "InviteToClanWithName", {
                "name": words[1], "inviteType": 0})["result"], "invited %s" % words[1])
        elif what == "join" and len(words) <= 2:
            clan_id = int(words[1]) if len(words) == 2 else self._first_clan_invitation()
            if clan_id:
                self._clan_result(self.player.call("ClansProtocol", "JoinClan",
                                                   {"clanId": clan_id})["result"],
                                  "joined clan %d" % clan_id)
        elif what == "members" and len(words) <= 2:
            members = self.player.call("ClansProtocol", "GetClanMembers", {
                "clanId": int(words[1]) if len(words) == 2 else 0})["members"]
            if not members:
                print("  nobody")
            for m in members:
                print("  %-20s %-9s %s" % (m["name"], ROLES.get(m["role"], "rank %d" % m["role"]),
                                           "online" if m["online"] else "offline"))
        elif what == "leave" and len(words) == 1:
            self._clan_result(self.player.call("ClansProtocol", "LeaveClan",
                                               {"pid": 0})["result"], "left the clan")
        elif not what:
            clan_id = self.player.call("ClansProtocol", "GetCurUserClan")["clanId"]
            if not clan_id:
                print("  in no clan")
                return
            tag, = self.player.call("ClansProtocol", "GetClanTags",
                                    {"clanIds": [clan_id]})["tags"]
            print("  clan %d [%s]" % (clan_id, tag))
        else:
            print("clan [create|invite|join|members|leave]")

    def _first_clan_invitation(self) -> int:
        invites = self.player.call("ClansProtocol", "GetClanInvitesWithNames",
                                   {"clanId": 0})["invites"]
        if not invites:
            print("no clan has invited you")
            return 0
        return invites[0]["clanId"]

    @staticmethod
    def _clan_result(result: int, done: str):
        if result == clan_model.OK:
            print(done)
        else:
            print("refused: 0x%X %s" % (result, clan_model.CODES.get(result, "?")))

    # ----- the profile: PlayerStatisticsProtocol --------------------------------------------
    def do_stats(self, line):
        """stats send BOARD STAT VALUE [OP] - one Int32 value; OP is ADD SUB SET MAX MIN or 0..4,
                                         SET by default. BOARD/C1/C2 gives the row's contexts,
                                         as board 2 (a class) and 3 (a class's slot) have them
        stats read BOARD [NAME] - every Int32 row of a board, yours by default"""
        words = line.split()
        what = words[0] if words else ""
        if what == "send" and len(words) in (4, 5):
            board, *contexts = [int(x, 0) for x in words[1].split("/")]
            ops = {name: code for code, name in stat_model.OPS.items()}
            op = words[4].upper() if len(words) == 5 else "SET"
            op = ops[op] if op in ops else int(op)
            self.player.call("PlayerStatisticsProtocol", "SendPlayerStatsInt32", {"statGroups": [{
                "board": board, "contexts": contexts,
                "stats": [{"statId": int(words[2], 0), "value": int(words[3], 0), "op": op}]}]})
            print("sent: board %s stat %s %s %s" % (words[1], words[2],
                                                    stat_model.OPS.get(op, op), words[3]))
        elif what == "read" and len(words) in (2, 3):
            pid = self.pid_of(words[2]) if len(words) == 3 else self.player.pid
            # No contexts, one and two: every shape of row the boards have, whatever their keys.
            queries = [{"board": int(words[1], 0), "contexts": [stat_model.ANY] * n,
                        "statIds": []} for n in range(3)]
            result, = self.player.call("PlayerStatisticsProtocol", "ReadPlayerStatsInt32",
                                       {"queries": queries, "pids": [pid]})["results"]
            if not result["rows"]:
                print("  nothing stored")
            for row in result["rows"]:
                print("  board %d %s: %s" % (row["board"], row["contexts"], ", ".join(
                    "%d=%d" % (s["statId"], s["value"]) for s in row["stats"])))
        else:
            print("stats send BOARD STAT VALUE [OP] | stats read BOARD [NAME]")

    # ----- a matchmade host: MapListProtocol ------------------------------------------------
    def do_maps(self, line):
        """maps categories - the game modes on offer, each with the bits it allows
        maps list MASK - the worlds a host with this mask may pick, e.g. maps list 0x4010"""
        words = line.split()
        what = words[0] if words else ""
        if what == "categories" and len(words) == 1:
            for category in self.player.call("MapListProtocol",
                                             "GetAvailableMapCategories")["categories"]:
                print("  %#07x %s" % (category, map_model.MODE_NAMES.get(
                    category & map_model.ANY_MODE, "?")))
        elif what == "list" and len(words) == 2:
            keys = self.player.call("MapListProtocol", "GetMapList",
                                    {"categoryId": int(words[1], 0)})["mapIds"]
            names = _map_names()
            for key in keys:
                print("  %08x %s" % (key, names.describe(key) if names else ""))
        else:
            print("maps categories | maps list MASK")

    # ----- anything else --------------------------------------------------------------------
    def do_call(self, line):
        """call PROTOCOL METHOD [JSON] - any call the schema knows, e.g.
        call PrivilegesProtocol GetPrivilegesEx {"localeCode": "en"}"""
        parts = line.split(None, 2)
        if len(parts) < 2:
            print("call PROTOCOL METHOD [JSON]")
            return
        method = int(parts[1]) if parts[1].isdigit() else parts[1]
        args = json.loads(parts[2]) if len(parts) > 2 else {}
        print(json.dumps(self.player.call(parts[0], method, args), indent=1, default=repr))

    def do_ping(self, _):
        """ping - one ping to the secure server"""
        self.player.secure.ping()
        print("pong")

    def do_quit(self, _):
        """quit - leave, the way a game that is closed leaves: it just stops talking"""
        self.stop.set()
        self.player.close()
        return True

    do_EOF = do_quit


def _props(values: dict) -> list:
    return [{"m_ID": k, "m_value": v} for k, v in values.items()]


def _map_names():
    """The server's own map table, to put names to world keys - when this runs beside it."""
    try:
        return map_model.Maps()
    except map_model.MapError:
        return None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--name", required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--auth-port", type=int, default=21030)
    parser.add_argument("--secure-port", type=int, default=21031)
    parser.add_argument("--config-port", type=int,
                        help="ask the configuration service on this port where the server is")
    args = parser.parse_args(argv)

    auth_port, secure_port = args.auth_port, args.secure_port
    if args.config_port:
        _, auth_port = url_address(fetch_config(args.host, args.config_port)["SandboxUrl"])
        secure_port = auth_port + 1
    player = Player(args.host, auth_port, secure_port, args.name).login()
    game = FakeGame(player)
    game.do_whoami("")
    try:
        game.cmdloop("type help for the commands")
    except KeyboardInterrupt:
        game.do_quit("")
    return 0


if __name__ == "__main__":
    sys.exit(main())
