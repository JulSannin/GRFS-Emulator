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


class TicketGranting(Service):
    protocol_id = protocols.TICKET_GRANTING
    protocol_name = "TicketGrantingProtocol"

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
    def _sign_in(self, ctx, name, tail: bool):
        client = ctx.client
        vport = ctx.packet.src
        name = (name or "").strip()
        if not name:
            raise RmcError(Errors.RV_INVALID_USERNAME, "login with no user name")

        if name == kerberos.TRACKING_NAME:
            pid = kerberos.TRACKING_PID
        else:
            pid = self.backend.accounts.pid_for(name)
            # The player's identity on this game. Only a real login sets it, so that the
            # telemetry user signing in from the same address cannot take it over.
            client.name = name
            client.pid = pid
            client.logged_in_at = self.backend.now()

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
