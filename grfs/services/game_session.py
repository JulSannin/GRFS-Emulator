"""GameSessionProtocol (0x2A): co-op, the squad and invitations.

The squad lobby and the co-op match are both GameSessions of type 1; MatchMaking is never
registered by this client. What each call is for, in the order a game makes them:

    hosting     21 RegisterURLs   1 CreateSession   8 AddParticipants (itself first)
                2 UpdateSession on every settings change
    searching   7 SearchSessions, then a probe of every host it was given
    joining     no call at all - the joiner connects to the host, and the host's
                8 AddParticipants is how the server learns of it
    inviting    12 SendInvitation -> notification 7005 to each recipient
                -> 14 GetInvitationsReceived -> 6 GetSession -> probe -> 17 AcceptInvitation
                18 DeclineInvitation
    leaving     5 LeaveSession (a member) or 23 AbandonSession (the host)
                9 RemoveParticipants (the host, for whoever dropped out)
    migrating   4 MigrateSession by the new host, then 11 GetParticipants, 8 and 9 to
                bring the list in line with its own
    friends     24 SearchSessionsWithParticipants on every refresh of the friends list

What was learned from the client's code, and what goes wrong without it:

* Every result is parsed by one function that reads attributes 3, 4, 5 and 6 with no null
  check. A result missing one crashes the game that asked - and that includes the friends list,
  which runs 24 behind every refresh. They are always sent.
* 5 and 6 are used slots, 3 and 4 the capacities; the client works out the free slots as 3 - 5
  and 4 - 6. The client sends 5 = 6 = 0 at create and never updates them, so the server keeps
  them from the participant lists (grfs.sessions).
* 2 UpdateSession sends only what changed, often one attribute. Merged by id, never replaced.
  Attribute 0x70 changes in play and is what the joiner connects to the host with.
* The key that arrives with an invitation has no type: the client builds it from the
  notification, which carries only the session id. Sessions are found by id alone.
* The notification only says "look": the client ignores its payload and asks for its
  invitations, taking the last one in the list as the new one.
* 24 must never fail. A failed step fails the whole friends-list refresh, which retries every
  five seconds and leaves the screen stale; an empty list is what "nobody is playing" looks like.
* 22 JoinSession is in the client but nothing calls it - the join path only probes the host.
  It is answered anyway, as a check that changes nothing.
* 4 MigrateSession is taken from anybody with the key. The new host may be a member the crashed
  host never added, while the crashed host still looks present.
* A game that crashed and came back is in no session. The server notices the new connection
  under the old pid before it answers anything about sessions (see _settle).

The matching rules for 7 are the server's own: the original ran them as scripts that never
shipped. These are the rules under which the client's two queries make sense - see
grfs.sessions.matches.
"""

from .. import protocols
from .. import sessions as model
from ..rmc.router import Errors, RmcError, Service
from .nat_traversal import parse_url

# The game's own notification types for this protocol (category 7).
INVITATION_RECEIVED = 7005
INVITATION_ACCEPTED = 7007

# How much of an invitation's message the notification carries; the client copies no more.
NOTIFICATION_TEXT = 64


class GameSession(Service):
    protocol_id = protocols.GAME_SESSION
    protocol_name = "GameSessionProtocol"

    def __init__(self, backend):
        super().__init__(backend)
        # The backend's, which it also takes a forgotten client out of.
        self.sessions = backend.sessions

    # ----- 21 RegisterURLs -------------------------------------------------------------------
    def RegisterURLs(self, ctx, stationURLs):
        """Where other players reach this one: the host URLs in every result for its sessions.
        Until a game sends these, the URLs it registered with the secure server stand in."""
        pid = self._me(ctx)
        urls = []
        for url in stationURLs or ():
            if not url:
                continue
            scheme, fields = parse_url(url)
            try:
                port = int(fields.get("port", "0"))
            except ValueError:
                port = 0
            if scheme.lower() not in ("udp", "prudp", "prudps") or not fields.get("address") \
                    or not 0 < port <= 65535:
                # A real client can register only PID/RVCID when its public address is not
                # available yet. That identifies a player, but is no destination to probe.
                self.log.info("%s registered an incomplete session URL, left out: %s",
                              ctx.client.name, url)
                continue
            urls.append(url)
        self.sessions.urls[pid] = urls
        self.log.info("%s registered %d session URL(s): %s", ctx.client.name,
                      len(self.sessions.urls[pid]), ", ".join(self.sessions.urls[pid]) or "-")

    # ----- 1 CreateSession -------------------------------------------------------------------
    def CreateSession(self, ctx, gameSession):
        pid = self._me(ctx)
        gameSession = gameSession or {}
        sent = gameSession.get("m_attributes") or []
        if not any(p.get("m_ID") == model.PUBLIC_SLOTS for p in sent) or \
                not any(p.get("m_ID") == model.PRIVATE_SLOTS for p in sent):
            self.log.info("%s created a session without both capacities; the missing one is 0",
                          ctx.client.name)
        session = self.sessions.create(pid, gameSession.get("m_typeID", 0), sent)
        self.log.info("%s hosts session %d (type %d): %s", ctx.client.name, session.id,
                      session.type_id, _attributes(session.attributes))
        return {"gameSessionKey": session.key}

    # ----- 2 UpdateSession -------------------------------------------------------------------
    def UpdateSession(self, ctx, gameSessionUpdate):
        gameSessionUpdate = gameSessionUpdate or {}
        session = self._session(gameSessionUpdate.get("m_sessionKey"))
        self._host_only(ctx, session, "update")
        refused = self.sessions.update(session, gameSessionUpdate.get("m_attributes"))
        if refused:
            self.log.info("session %d: %s may not set %s, kept the server's", session.id,
                          ctx.client.name, ", ".join("0x%X" % a for a in refused))
        self.log.info("session %d updated: %s", session.id, _attributes(session.attributes))

    # ----- 3 DeleteSession -------------------------------------------------------------------
    def DeleteSession(self, ctx, gameSessionKey):
        """The client has no caller for this; it is answered for the host and nobody else."""
        session = self._session(gameSessionKey)
        self._host_only(ctx, session, "delete")
        self.sessions.delete(session)
        self.log.info("%s deleted session %d", ctx.client.name, session.id)

    # ----- 4 MigrateSession ------------------------------------------------------------------
    def MigrateSession(self, ctx, gameSessionKey):
        """The caller hosts the session from now on. The key stays the same - the client takes
        whatever key comes back and then makes the participant list match its own.

        Never refused for who the caller is. A host that crashed is still in the session and
        still looks online here for a minute and a half, and it may have gone before it added
        the member who takes over: refusing that member - by not being listed, or by the host
        looking present - loses the match for everyone in it. So a session that exists and a
        caller who is logged in are all it takes.
        """
        pid = self._me(ctx)
        session = self._session(gameSessionKey)
        before = session.owner
        self.sessions.migrate(session, pid)
        self.log.info("session %d migrated to %s (host was %s)", session.id, ctx.client.name,
                      self._name(before) if before else "nobody")
        return {"gameSessionKeyMigrated": session.key}

    # ----- 5 LeaveSession / 23 AbandonSession ------------------------------------------------
    def LeaveSession(self, ctx, gameSessionKey):
        """A member going. Sent by a host too, now and then; that is an abandon."""
        self._leave(ctx, gameSessionKey, "left")

    def AbandonSession(self, ctx, gameSessionKey):
        """The host going. The session stays for whoever migrates it, while anybody is in it."""
        self._leave(ctx, gameSessionKey, "abandoned")

    # ----- 6 GetSession / 25 GetSessions -----------------------------------------------------
    def GetSession(self, ctx, gameSessionKey):
        session = self._session(gameSessionKey)
        result = self._result(session)
        self.log.info("%s asked for session %d by %s; host URLs: %s", ctx.client.name,
                      session.id, self._name(session.owner),
                      ", ".join(result["m_hostURLs"]) or "none")
        return {"searchResult": result}

    def GetSessions(self, ctx, gameSessionKeys):
        """The ones that still exist; a key for a session that is gone is left out."""
        self._settle()
        found = [self.sessions.get((k or {}).get("m_sessionID", 0)) for k in gameSessionKeys or []]
        return {"searchResults": [self._result(s) for s in found if s is not None]}

    # ----- 7 SearchSessions ------------------------------------------------------------------
    def SearchSessions(self, ctx, gameSessionQuery):
        pid = self._me(ctx)
        query = gameSessionQuery or {}
        query_id = query.get("m_queryID", 0)
        if query_id not in model.QUERIES:
            raise RmcError(Errors.SESSION_INVALID_QUERY_ID, "query %d" % query_id)
        self._settle()
        found = self.sessions.search(pid, query.get("m_typeID", 0), query_id,
                                     query.get("m_parameters"), hosted=self._hosted)
        self.log.info("%s searched (query %d: %s) -> %s", ctx.client.name, query_id,
                      _attributes(dict(model._pairs(query.get("m_parameters")))),
                      ", ".join("%d by %s" % (s.id, self._name(s.owner)) for s in found) or
                      "nothing")
        return {"searchResults": [self._result(s) for s in found]}

    # ----- 24 SearchSessionsWithParticipants -------------------------------------------------
    def SearchSessionsWithParticipants(self, ctx, gameSessionTypeID, participantIDs):
        """Which of these players (the caller's friends) are in a session, and which one. Never
        an error - see the module docstring.

        The client matches m_participantIDs against its own friends and passes over anybody
        else, so every participant is listed, not only the ones asked about."""
        self._settle()
        found = [s for s in self.sessions.with_participants(gameSessionTypeID, participantIDs)
                 if self._hosted(s)]
        if found:
            self.log.info("%s: friends in session %s", ctx.client.name or ctx.client.address,
                          ", ".join(str(s.id) for s in found))
        return {"searchResults": [dict(self._result(s), m_participantIDs=s.participants())
                                  for s in found]}

    # ----- 22 JoinSession --------------------------------------------------------------------
    def JoinSession(self, ctx, gameSessionKey):
        """Is there room? Nothing is counted here: the host adds the joiner once it has
        connected, and counting it twice would make the session look fuller than it is."""
        pid = self._me(ctx)
        session = self._session(gameSessionKey)
        if not session.has(pid):
            free_public, _ = session.free()
            if free_public <= 0:
                raise RmcError(Errors.SESSION_NO_PUBLIC_SLOT, "session %d is full" % session.id)
        self.log.info("%s may join session %d", ctx.client.name, session.id)

    # ----- 8 AddParticipants / 9 RemoveParticipants ------------------------------------------
    def AddParticipants(self, ctx, gameSessionKey, publicParticipantIDs=(),
                        privateParticipantIDs=()):
        """Never refused for want of room and never drops anybody: the host has already let them
        in, and its own count is the one that decides."""
        session = self._session(gameSessionKey)
        self._host_only(ctx, session, "add participants to")
        added = self.sessions.add(session, publicParticipantIDs, privateParticipantIDs)
        # A joiner need not have called here itself; which game it joined in is noted now, so
        # that a restart of it is noticed as for anybody else.
        for joined in added:
            current = self.backend.clients.by_pid(joined)
            if current is not None:
                self._playing(joined, current)
        self.log.info("session %d: %s added %s -> public %d/%d, private %d/%d", session.id,
                      ctx.client.name, ", ".join(self._name(p) for p in added) or "nobody new",
                      session.used()[0], session.capacity()[0], session.used()[1],
                      session.capacity()[1])

    def RemoveParticipants(self, ctx, gameSessionKey, participantIDs=()):
        session = self._session(gameSessionKey)
        self._host_only(ctx, session, "remove participants from")
        removed = self.sessions.remove(session, participantIDs)
        self.log.info("session %d: %s removed %s -> public %d/%d, private %d/%d", session.id,
                      ctx.client.name, ", ".join(self._name(p) for p in removed) or "nobody",
                      session.used()[0], session.capacity()[0], session.used()[1],
                      session.capacity()[1])

    # ----- 10 GetParticipantCount / 11 GetParticipants / 26 GetParticipantsURLs --------------
    def GetParticipantCount(self, ctx, gameSessionKey):
        return {"count": len(self._session(gameSessionKey).participants())}

    def GetParticipants(self, ctx, gameSessionKey, resultRange=None):
        """What a new host compares its own list with after migrating."""
        session = self._session(gameSessionKey)
        return {"participants": [self._participant(p)
                                 for p in _page(session.participants(), resultRange)]}

    def GetParticipantsURLs(self, ctx, gameSessionKey, participantIDs=()):
        session = self._session(gameSessionKey)
        return {"participants": [self._participant(p) for p in participantIDs or ()
                                 if session.has(p)]}

    # ----- 12 SendInvitation -----------------------------------------------------------------
    def SendInvitation(self, ctx, invitation):
        """Kept for each recipient, and each one that is here is told with 7005.

        The client reads nothing from the notification but its type - it then asks for its
        invitations - so the fields carry what they were meant to (inviter, session type, session
        id, message) and nothing depends on them. A recipient with no account here is skipped
        rather than failing the call for the others.
        """
        pid = self._me(ctx)
        invitation = invitation or {}
        session = self._session(invitation.get("m_sessionKey"))
        message = invitation.get("m_message") or ""
        for recipient in dict.fromkeys(invitation.get("m_recipientPIDs") or ()):
            if recipient == pid:
                continue
            name = self.backend.accounts.name_for(recipient)
            if not name:
                self.log.info("%s invited pid 0x%X, which has no account here", ctx.client.name,
                              recipient)
                continue
            self.sessions.invite(session, pid, recipient, message)
            told = self.backend.notify(recipient, INVITATION_RECEIVED, source=pid,
                                       param1=session.type_id, param2=session.id,
                                       text=message[:NOTIFICATION_TEXT], param3=0)
            self.log.info("%s invited %s to session %d%s", ctx.client.name, name, session.id,
                          "" if told else " (not here; it waits)")

    # ----- 13..16 the invitations a player has -----------------------------------------------
    def GetInvitationReceivedCount(self, ctx, gameSessionTypeID):
        return {"count": len(self.sessions.received(self._me(ctx), gameSessionTypeID))}

    def GetInvitationsReceived(self, ctx, gameSessionTypeID, resultRange=None):
        """Oldest first: the client shows the last one as the one that just arrived."""
        mine = self.sessions.received(self._me(ctx), gameSessionTypeID)
        return {"invitations": [i.received() for i in _page(mine, resultRange)]}

    def GetInvitationSentCount(self, ctx, gameSessionTypeID):
        return {"count": len(self.sessions.sent(self._me(ctx), gameSessionTypeID))}

    def GetInvitationsSent(self, ctx, gameSessionTypeID, resultRange=None):
        mine = self.sessions.sent(self._me(ctx), gameSessionTypeID)
        return {"invitations": [i.sent() for i in _page(mine, resultRange)]}

    # ----- 17 AcceptInvitation / 18 DeclineInvitation / 19 CancelInvitation ------------------
    def AcceptInvitation(self, ctx, gameSessionInvitation):
        """Sent after the invitee has looked the session up and reached its host. The invitation
        is used up either way; the session must still exist and have a slot for them. Joining is
        then the host's AddParticipants, as for any join. The inviter is told with 7007, which
        its client only logs."""
        pid = self._me(ctx)
        invitation = gameSessionInvitation or {}
        session_id = (invitation.get("m_sessionKey") or {}).get("m_sessionID", 0)
        sender = invitation.get("m_senderPID", 0)
        self.sessions.withdraw(pid, sender, session_id)
        session = self._session(invitation.get("m_sessionKey"))
        if not session.has(pid):
            free_public, free_private = session.free()
            if free_public <= 0 and free_private <= 0:
                raise RmcError(Errors.SESSION_NO_PRIVATE_SLOT if session.capacity()[1]
                               else Errors.SESSION_NO_PUBLIC_SLOT,
                               "session %d is full" % session.id)
        self.log.info("%s accepted %s's invitation to session %d", ctx.client.name,
                      self._name(sender), session.id)
        if sender and sender != pid:
            self.backend.notify(sender, INVITATION_ACCEPTED, source=pid,
                                param1=session.type_id, param2=session.id)

    def DeclineInvitation(self, ctx, gameSessionInvitation):
        pid = self._me(ctx)
        invitation = gameSessionInvitation or {}
        session_id = (invitation.get("m_sessionKey") or {}).get("m_sessionID", 0)
        gone = self.sessions.withdraw(pid, invitation.get("m_senderPID", 0), session_id)
        self.log.info("%s declined %s's invitation to session %d%s", ctx.client.name,
                      self._name(invitation.get("m_senderPID", 0)), session_id,
                      "" if gone else " (there was none)")

    def CancelInvitation(self, ctx, gameSessionInvitation):
        """The client has no caller for this; the sender takes an invitation back."""
        pid = self._me(ctx)
        invitation = gameSessionInvitation or {}
        session_id = (invitation.get("m_sessionKey") or {}).get("m_sessionID", 0)
        recipient = invitation.get("m_recipientPID", 0)
        gone = self.sessions.withdraw(recipient, pid, session_id)
        self.log.info("%s took back the invitation of %s to session %d%s", ctx.client.name,
                      self._name(recipient), session_id, "" if gone else " (there was none)")

    # ----- 20 SendTextMessage ----------------------------------------------------------------
    def SendTextMessage(self, ctx, gameSessionMessage):
        """The client has no caller for this and no notification type to show one with, so
        the message is accepted and logged, and goes no further."""
        message = gameSessionMessage or {}
        session = self._session(message.get("m_sessionKey"))
        self.log.info("%s said to session %d: %r", ctx.client.name, session.id,
                      message.get("m_message") or "")

    # -----------------------------------------------------------------------------------------
    def _me(self, ctx) -> int:
        if not ctx.client.pid:
            raise RmcError(Errors.RV_NOT_AUTHENTICATED, "a session call before logging in")
        self._playing(ctx.client.pid, ctx.client)
        return ctx.client.pid

    def _settle(self) -> None:
        """Take every game that has been restarted since it last called out of what it was in,
        before answering anybody with what it hosted.

        A restarted game logs in under the same pid from a new connection, and the login drops
        the old connection's record - so it is never forgotten the usual way, and it is online
        again at once. Without this its sessions would be offered with the new game's URLs
        until the new game happened to call here."""
        for pid in self.sessions.players():
            current = self.backend.clients.by_pid(pid)
            if current is not None:
                self._playing(pid, current)

    def _playing(self, pid: int, game) -> None:
        for session in self.sessions.seen(pid, game):
            self.log.info("%s was restarted: out of session %d%s", self._name(pid), session.id,
                          "" if self.sessions.get(session.id) else " - nobody is left, it is gone")

    def _session(self, key) -> model.Session:
        self._settle()
        session_id = (key or {}).get("m_sessionID", 0)
        session = self.sessions.get(session_id)
        if session is None:
            raise RmcError(Errors.SESSION_INVALID_SESSION_KEY, "no session %d" % session_id)
        return session

    def _host_only(self, ctx, session, what: str) -> None:
        if session.owner != self._me(ctx):
            raise RmcError(Errors.SESSION_HOST_PRIVILEGE_REQUIRED,
                           "%s may not %s session %d, hosted by %s" % (
                               ctx.client.name, what, session.id,
                               self._name(session.owner) if session.owner else "nobody"))

    def _leave(self, ctx, key, how: str) -> None:
        pid = self._me(ctx)
        session = self._session(key)
        was_host = session.owner == pid
        gone = self.sessions.leave(session, pid)
        self.log.info("%s %s session %d%s", ctx.client.name, how, session.id,
                      " - nobody is left, it is gone" if gone else
                      " - it waits for a new host" if was_host else "")

    def _hosted(self, session) -> bool:
        return bool(session.owner) and self.backend.is_online(session.owner)

    def _urls(self, pid: int) -> list:
        urls = self.sessions.urls.get(pid)
        if urls:
            return list(urls)
        client = self.backend.clients.by_pid(pid) if pid else None
        return list(client.urls) if client is not None else []

    def _result(self, session) -> dict:
        return {"m_sessionKey": session.key, "m_hostPID": session.owner,
                "m_hostURLs": self._urls(session.owner),
                "m_attributes": session.attribute_list()}

    def _participant(self, pid: int) -> dict:
        return {"m_PID": pid, "m_name": self._name(pid), "m_stationURLs": self._urls(pid)}

    def _name(self, pid: int) -> str:
        return self.backend.accounts.name_for(pid) or "pid 0x%X" % pid


def _page(items: list, result_range) -> list:
    """A ResultRange applied: from offset, at most size; a size of 0 means all of it."""
    if not isinstance(result_range, dict):
        return list(items)
    offset = result_range.get("m_uiOffset", 0) or 0
    size = result_range.get("m_uiSize", 0) or len(items)
    return list(items[offset:offset + size])


def _attributes(values: dict) -> str:
    return " ".join("%X=%d" % (aid, v) for aid, v in sorted(values.items())) or "-"
