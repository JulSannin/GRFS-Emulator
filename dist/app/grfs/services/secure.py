"""SecureConnectionProtocol (0x0B): registering a client's own addresses.

Straight after connecting to the secure server the game registers every address it could be
reached at - one station URL per local adapter - and gets back a connection id, which it then
uses as its own RVCID for the rest of the session. This is the call the whole login hangs off:
if the client does not accept the reply it never gets as far as anything else, and the game drops
to offline mode without saying why.

Two things learned the hard way:

* The URL in the reply is the *server's* address as this client sees it, not the client's own.
  That reading comes from a working capture - the client takes this URL and re-registers it back
  as one of its own station URLs, and the only value it was ever seen to accept pointed here.
* The telemetry connection registers too, with its own addresses, and it must not overwrite the
  player's. When it did, the session the player created a moment later carried telemetry URLs
  and nobody could connect to them.
"""

from .. import kerberos
from .. import protocols
from ..rmc.router import SUCCESS, Errors, RmcError, Service

# Where connection ids start. The live servers' first was 78, and a verified run of the previous
# attempt used it, so the first player on a fresh server still gets exactly that.
FIRST_CONNECTION_ID = 78


class SecureConnection(Service):
    protocol_id = protocols.SECURE_CONNECTION
    protocol_name = "SecureConnectionProtocol"

    def __init__(self, backend):
        super().__init__(backend)
        self._next_id = FIRST_CONNECTION_ID

    # ----- 1 Register ------------------------------------------------------------------------
    def Register(self, ctx, vecMyURLs):
        return self._register(ctx, vecMyURLs)

    # ----- 4 RegisterEx ----------------------------------------------------------------------
    def RegisterEx(self, ctx, vecMyURLs, hCustomData=None):
        """The same thing with the login data attached, which is what this game actually sends.

        hCustomData is UbiAuthenticationLoginCustomData again - the credentials in it are skipped
        while decoding, so only the name is here, and the name is already known from the login.
        """
        name = hCustomData.get("m_userName") if isinstance(hCustomData, dict) else None
        if name and ctx.client.name and name != ctx.client.name:
            self.log.info("%s registered under the name %s", ctx.client.name, name)
        return self._register(ctx, vecMyURLs)

    # ----- 3 RequestURLs ---------------------------------------------------------------------
    def RequestURLs(self, ctx, cidTarget, pidTarget):
        """Where to reach another player. Asked for when connecting to a peer."""
        target = (self.backend.clients.by_pid(pidTarget) if pidTarget
                  else self._by_connection_id(cidTarget))
        if target is None or not target.urls:
            self.log.info("%s asked where to reach pid 0x%X / cid %d, which is not here",
                          ctx.client.name or ctx.client.address, pidTarget, cidTarget)
            return {"retval": False, "plstURLs": []}
        self.log.info("%s asked where to reach %s -> %d URL(s)",
                      ctx.client.name or ctx.client.address, target.name, len(target.urls))
        return {"retval": True, "plstURLs": list(target.urls)}

    # ----- 2 RequestConnectionData -----------------------------------------------------------
    def RequestConnectionData(self, ctx, cidTarget, pidTarget):
        target = (self.backend.clients.by_pid(pidTarget) if pidTarget
                  else self._by_connection_id(cidTarget))
        if target is None or not target.urls:
            return {"retval": False, "pvecConnectionsData": []}
        return {"retval": True, "pvecConnectionsData": [
            {"m_StationUrl": url, "m_ConnectionID": target.rvcid} for url in target.urls]}

    # -----------------------------------------------------------------------------------------
    def _register(self, ctx, urls):
        client = ctx.client
        vport = ctx.packet.src
        auth = client.auth.get(vport)
        if auth is None:
            raise RmcError(Errors.RV_NOT_AUTHENTICATED,
                           "registration on a connection that never logged in")

        if not client.rvcid:
            client.rvcid = self._next_id
            self._next_id += 1

        urls = [u for u in (urls or []) if u]
        # The client keeps the urlPublic this answers with in the same list it registers from,
        # so a second registration from the same game process (back to the menu and in again)
        # hands the server's own prudps:/ URL back as one of the game's. Nobody reaches a game
        # there; a game's own addresses are prudp:/ and udp:/.
        ours = [u for u in urls if u.lower().startswith("prudps:")]
        if ours:
            self.log.info("%s registered %d of the server's own URL(s) back as its own -"
                          " left out", auth.name, len(ours))
            urls = [u for u in urls if u not in ours]
        if auth.name == kerberos.TRACKING_NAME:
            # Kept for the record, never used to reach anybody.
            self.log.debug("telemetry connection registered %d URL(s); the player's are left"
                           " alone", len(urls))
        else:
            # A game that registers again - new addresses, same game - has not signed in again.
            signing_in = not client.urls
            # Stored exactly as sent. Whether an address a client claims is one anybody can
            # reach is a separate question, answered where the URLs are handed out, not here.
            client.urls = urls
            self.log.info("%s registered %d station URL(s) as connection %d: %s", auth.name,
                          len(urls), client.rvcid, ", ".join(urls) or "-")
            if signing_in:
                self.tell_friends(client)

        return {"retval": SUCCESS, "pidConnectionID": client.rvcid,
                "urlPublic": self.backend.station_url(client, client.rvcid)}

    def tell_friends(self, client) -> None:
        """A player has signed in, or gone: their friends' screens keep the old online state
        until something makes those refetch, and nothing else would - the client has no
        presence notification. So each friend gets the same "friend changed" notification a
        change of friendship sends (see services.friends): it only marks their list for
        refetching, and the refetch reads the status byte as it is now. A request either way is
        not a friend - the screen shows no online state for one - so nobody else is told.

        Called here on sign-in, and by Backend.notice_departures when a game goes quiet past
        the presence window or comes back from that."""
        if not client.name or not client.pid:
            return
        told = [f.name for f in self.backend.friends.friends_of(client.name)
                if self.backend.notify(f.pid, 1000, source=client.pid, param1=client.pid,
                                       param2=1, text="")]
        if told:
            self.log.info("%s is %s: told %s", client.name,
                          "here" if self.backend.is_online(client.pid) else "gone",
                          ", ".join(told))

    def _by_connection_id(self, cid: int):
        for client in self.backend.clients:
            if client.rvcid == cid:
                return client
        return None
