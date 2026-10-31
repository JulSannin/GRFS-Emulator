"""A stand-in for the game: logs in over real sockets and says what happened.

The thing the previous attempt never had. Every check cost a launch of the game, a look at a
menu, and a guess about which of the two machines was at fault; a protocol mistake took an
evening to find. This walks the same path the game walks - handshake, log in, ask for a ticket,
prove it on the secure server, register, send a NAT probe - and prints each step, so a change can
be checked in a second and from one machine.

    python tools/fake_client.py                      # against a server on this machine
    python tools/fake_client.py --host 192.168.1.8   # against the other PC
    python tools/fake_client.py --config             # ask the configuration service first

The client itself is grfs.fakegame, the same one the tests use. For a session to drive by hand
- a second player on the friends screen, say - see tools/fake_game.py.
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from grfs.fakegame import Peer, Player, fetch_config, url_address          # noqa: E402

GREEN, RED, DIM, OFF = "\033[32m", "\033[31m", "\033[90m", "\033[0m"


def ok(text):
    print(" %s+%s %s" % (GREEN, OFF, text))


def bad(text):
    print(" %s!%s %s" % (RED, OFF, text))


def note(text):
    print("   %s%s%s" % (DIM, text, OFF))


def check_login(host, auth_port, secure_port, name):
    failures = 0
    print("\nsigning in as %s :%d / :%d" % (name, auth_port, secure_port))
    player = Player(host, auth_port, secure_port, name)
    try:
        player.login()
        ok("logged in as %s, pid 0x%X" % (name, player.pid))
        ok("the ticket decrypts: session key %s..., server pid 0x%X"
           % (player.session_key[:4].hex(), player.server_pid))
        ok("secure server at %s" % player.secure_url)
        told = url_address(player.secure_url)[0]
        if told in ("0.0.0.0", "", "255.255.255.255"):
            bad("that address is not one anybody can connect to")
            failures += 1
        if player.requested_ticket == player.ticket:
            ok("the ticket came back identical")
        else:
            bad("the ticket came back DIFFERENT")
            failures += 1
        code, answer = player.challenge
        ok("it answered the challenge: %08X" % answer)
        ok("registered as connection %d" % player.rvcid)
        ok("it calls us %s" % player.register_reply["urlPublic"])
        for connection in (player.auth, player.secure):
            if connection.bad_signatures:
                bad("%d packet(s) signed for another connection" % connection.bad_signatures)
                failures += 1
    except Exception as e:
        bad(str(e))
        failures += 1
    finally:
        player.close()
    return failures


def check_nat(host, port):
    print("\nNAT echo :%d" % port)
    with Peer(bind="127.0.0.1" if host.startswith("127.") else "0.0.0.0") as peer:
        try:
            url = peer.probe(host, port)
        except ValueError as e:
            bad(str(e))
            return 1
    if url is None:
        bad("no answer to the probe")
        return 1
    ok("it told us our address: %s" % url)
    return 0


def check_friends(host, auth_port, secure_port, names):
    """Two players find each other, ask, accept, and see each other in the list.

    This is the path to co-op: the game looks for a friend's game by asking which sessions its
    contacts are in, so nothing can be joined until this works.
    """
    print("\nfriends")
    one = two = None
    failures = 0
    try:
        one = Player(host, auth_port, secure_port, names[0]).login()
        two = Player(host, auth_port, secure_port, names[1]).login()
        ok("%s (pid 0x%X) and %s (pid 0x%X) are signed in" % (one.name, one.pid, two.name,
                                                              two.pid))
        # Start from nothing, so running this twice does the same thing twice.
        one.call("FriendsProtocol", "ClearRelationship", {"uiPlayer": two.pid})
        two.call("FriendsProtocol", "ClearRelationship", {"uiPlayer": one.pid})

        found = [f["m_strName"] for f in one.call("SearchFriendsProtocol", "SearchFriends",
                                                  {"partOfName": two.name})["foundList"]]
        if two.name in found:
            ok("%s searched and found %s" % (one.name, ", ".join(found)))
        else:
            bad("%s searched for %s and found %s" % (one.name, two.name, found or "nobody"))
            failures += 1

        asked = one.call("FriendsProtocol", "AddFriendByName", {
            "strPlayerName": two.name, "uiDetails": 1, "strMessage": "co-op?"})["retval"]
        ok("asked: %s" % asked) if asked else bad("the request was refused")
        failures += 0 if asked else 1

        # INVITATIONS is GetRelationships' rel 3. GetDetailedList(3) is the BLOCKLIST, and the
        # request must not be there as well.
        waiting = two.relationships().get(one.name)
        if waiting and waiting[0] == 3:
            ok("%s sees an invitation from %s (%s)" % (two.name, one.name,
                                                       "online" if waiting[1] else "offline"))
        else:
            bad("%s sees no invitation (%s)" % (two.name, waiting))
            failures += 1
        blocked = [b["m_strName"] for b in two.call("FriendsProtocol", "GetDetailedList", {
            "byRelationship": 3, "bReversed": False})["lstFriendsList"]]
        if one.name in blocked:
            bad("%s also sees %s on the BLOCKLIST" % (two.name, one.name))
            failures += 1
        messages = two.call("FriendMessagesExtensionServiceProtocol",
                            "ReceiveFriendInvitationMessages")["messages"]
        note("with the message: %s" % (messages[0]["m_message"] if messages else "none"))

        if two.call("FriendsProtocol", "AcceptFriendship", {"uiPlayer": one.pid})["retval"]:
            ok("%s accepted" % two.name)
        else:
            bad("accepting failed")
            failures += 1

        for player, other in ((one, two), (two, one)):
            seen = player.relationships()
            if seen.get(other.name, (0,))[0] == 1:
                ok("%s has %s as a friend, %s" % (player.name, other.name,
                                                  "online" if seen[other.name][1] else "offline"))
            else:
                bad("%s sees %s" % (player.name, seen))
                failures += 1

        rows = one.call("UbiFriendsProtocol", "GetRelationshipsList")["relationshipsList"]
        ids = [r["m_to"]["m_ubiAccountId"] for r in rows]
        resolved = dict(one.call("UbiAccountManagementProtocol",
                                 "LookupUsernamesByUbiAccountIDs",
                                 {"UbiAccountIds": ids})["Usernames"])
        if resolved:
            ok("the Uplay tab resolves: %s" % ", ".join("%s -> %s" % kv
                                                        for kv in resolved.items()))
        else:
            bad("the Uplay tab got no names back for %s" % ids)
            failures += 1
    except Exception as e:
        bad(str(e))
        failures += 1
    finally:
        for player in (one, two):
            if player is not None:
                player.close()
    return failures


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--auth-port", type=int, default=21030)
    parser.add_argument("--secure-port", type=int, default=21031)
    parser.add_argument("--config-port", type=int, default=80)
    parser.add_argument("--name", default="fakeclient")
    parser.add_argument("--config", action="store_true",
                        help="ask the configuration service where the server is, as the game does")
    parser.add_argument("--nat", action="store_true",
                        help="also send a NAT echo probe from a socket of its own")
    parser.add_argument("--friends", action="store_true",
                        help="also sign in two players and have them become friends; this adds"
                             " two accounts to the server's register")
    parser.add_argument("--friend-names", nargs=2, metavar=("A", "B"),
                        default=["fake-a", "fake-b"], help="the two accounts --friends uses")
    args = parser.parse_args(argv)

    auth_port, secure_port = args.auth_port, args.secure_port
    failures = 0
    print("\n%sGRFS emulator check%s - %s" % ("\033[1m", OFF, args.host))

    if args.config:
        print("\nconfiguration service")
        try:
            url = fetch_config(args.host, args.config_port).get("SandboxUrl", "")
            ok("SandboxUrl = %s" % url)
            host, auth_port = url_address(url)
            secure_port = auth_port + 1
            if host != args.host:
                note("it points at %s; using that for the ports only" % host)
            note("so the authentication server is on %d and the secure one on %d"
                 % (auth_port, secure_port))
        except Exception as e:
            bad("no configuration: %s" % e)
            failures += 1

    failures += check_login(args.host, auth_port, secure_port, args.name)
    if args.nat:
        failures += check_nat(args.host, secure_port)
    if args.friends:
        failures += check_friends(args.host, auth_port, secure_port, args.friend_names)

    print("\n%s%s%s\n" % (RED if failures else GREEN,
                          "%d step(s) failed" % failures if failures else "everything answered",
                          OFF))
    return 1 if failures else 0



if __name__ == "__main__":
    sys.exit(main())
