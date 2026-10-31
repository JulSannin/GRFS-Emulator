"""TicketGrantingProtocol (0x0A): signing in and handing out tickets.

This is the first thing a game does and everything else depends on it. Two different users sign
in from one game: the player, with LoginEx, carrying their Ubisoft name; and "Tracking", the
telemetry connection, with Login. Each gets its own ticket and session key, kept against the
virtual port it arrived on.

The reply says where the secure server is, and that address is worked out from the endpoint the
request came from (Backend.address_for) - never from a setting. A wrong address here does not
produce an error: the game simply never registers, loops back to logging in, and settles into
offline mode.

The player's Ubisoft password and online key are in the request. They are skipped while decoding
and never stored or logged (rmc.ddl.SENSITIVE); nothing here needs them, because it is Ubisoft
Connect that checks them, not this server.
"""

from .. import kerberos
from .. import protocols
from ..prudp.connection import AuthState
from ..rmc.router import SUCCESS, Errors, RmcError, Service

# A build string. The field is free text and the client only shows it in its own logs; the live
# servers sent one, so this sends one too.
BUILD = "GRFS-Emulator"

# Four bytes the live servers appended to the LoginEx reply, past the last field the game's own
# schema declares for it (the plain Login reply ends where the schema says). Their meaning is
# unknown; they are reproduced because this exact byte sequence is what the client was verified
# to accept, and the login is the worst possible place to improvise.
LOGINEX_TAIL = b"\x01\x00\x00\x00"

# twin_logins: how recently a game must have been heard from to count as still running when a
# second one signs in under its name. The game sends something about once a second; a game that
# was closed and started again takes far longer than this to get back to its login.
TWIN_LIVE = 10.0


class TicketGranting(Service):
    protocol_id = protocols.TICKET_GRANTING
    protocol_name = "TicketGrantingProtocol"

    def __init__(self, backend):
        super().__init__(backend)
        # twin_logins: (name, source address) -> the name that game was given, so that each copy
        # comes back under its own name after a restart.
        self._twins = {}

    # ----- 1 Login ---------------------------------------------------------------------------
    def Login(self, ctx, strUserName):
        """Plain login, used by the telemetry connection as the user "Tracking"."""
        return self._sign_in(ctx, strUserName, tail=False)

    # ----- 2 LoginEx -------------------------------------------------------------------------
    def LoginEx(self, ctx, strUserName, oExtraData=None):
        """The player's login. oExtraData is UbiAuthenticationLoginCustomData - the name is taken
        from it only when the plain parameter is empty."""
        name = strUserName
        if not name and isinstance(oExtraData, dict):
            name = oExtraData.get("m_userName")
        return self._sign_in(ctx, name, tail=True)

    # ----- 3 RequestTicket -------------------------------------------------------------------
    def RequestTicket(self, ctx, idSource, idTarget):
        """The ticket for the secure server, asked for right after logging in.

        The same ticket the login already returned: the live servers sent identical bytes for
        both, and it is the client's own copy either way - it cannot read it, it only forwards it.
        """
        auth = ctx.client.auth.get(ctx.packet.src)
        if auth is None:
            raise RmcError(Errors.RV_NOT_AUTHENTICATED,
                           "a ticket was asked for on a connection that never logged in")
        if auth.pid != idSource:
            self.log.warning("%s asked for a ticket for pid 0x%X on the connection logged in as"
                             " 0x%X (%s) - sending that one's", ctx.client.address, idSource,
                             auth.pid, auth.name)
        if idTarget != self.backend.server_pid:
            self.log.info("%s asked for a ticket to server 0x%X, ours is 0x%X", auth.name,
                          idTarget, self.backend.server_pid)
        return {"retval": SUCCESS, "bufResponse": auth.ticket}

    # -----------------------------------------------------------------------------------------
    def _twin_name(self, client, name: str) -> str:
        """twin_logins: the name a second copy of the game signed in to the same account plays
        under - name_2, name_3 - each with a pid of its own.

        Ubisoft Connect gives every copy of the game on one machine the same account, and one
        account is one player here: the second login would take the first one's place. With two
        copies running for a co-op test the second has to be somebody else, or the session it
        joins already has it as the host.

        A copy is the second one when another game signed in under this name is still running,
        from another endpoint. The name it got is remembered against its address, so a copy on an
        address of its own (tools/launch_game.py --ip) gets the same name after a restart.
        """
        key = (name.lower(), client.address)
        remembered = self._twins.get(key)
        now = self.backend.now()

        def running(candidate):
            return any(other is not client and other.name
                       and other.name.lower() == candidate.lower()
                       and now - other.last_seen < TWIN_LIVE for other in self.backend.clients)

        if remembered and not running(remembered):
            chosen = remembered
        else:
            chosen, n = name, 1
            while running(chosen):
                n += 1
                chosen = "%s_%d" % (name, n)
        self._twins[key] = chosen
        if chosen != name:
            self.log.info("%s signed in as %s while a game with that name is running - this one"
                          " is %s (twin_logins)", client.address, name, chosen)
        return chosen

    def _sign_in(self, ctx, name, tail: bool):
        client = ctx.client
        vport = ctx.packet.src
        name = (name or "").strip()
        if not name:
            raise RmcError(Errors.RV_INVALID_USERNAME, "login with no user name")

        if name == kerberos.TRACKING_NAME:
            pid = kerberos.TRACKING_PID
        else:
            if self.backend.config.bool("twin_logins"):
                name = self._twin_name(client, name)
            pid = self.backend.accounts.pid_for(name)
            # The player's identity on this game. Only a real login sets it, so that the
            # telemetry user signing in from the same address cannot take it over.
            client.name = name
            client.pid = pid
            client.logged_in_at = self.backend.now()
            # A game that was restarted comes back from a different source port, so the record
            # of the port it used before is dead. Leaving it in place means two records for one
            # player, and presence then depends on which of them is looked at first.
            for stale in self.backend.clients.drop_stale_with_pid(client):
                self.log.info("forgetting %s's earlier connection from %s:%d", name,
                              *stale.endpoint)

        session_key = kerberos.new_session_key()
        ticket = kerberos.Ticket(pid, self.backend.server_pid, session_key, kerberos.new_blob())
        blob = ticket.encode(kerberos.password_for(name))
        client.auth[vport] = AuthState(name, pid, session_key, blob)

        url = self.backend.secure_url(client)
        body = ctx.encode({
            "retval": SUCCESS,
            "pidPrincipal": pid,
            "pbufResponse": blob,
            "pConnectionData": {
                "m_urlRegularProtocols": url,
                "m_lstSpecialProtocols": [],
                "m_urlSpecialProtocols": "",
            },
            "strReturnMsg": BUILD,
        })
        if tail:
            body += LOGINEX_TAIL
        self.log.info("%s signed in as %s (pid 0x%X) on connection %02X; secure server at %s",
                      client.address, name, pid, vport, url)
        return ctx.reply_raw(body)
