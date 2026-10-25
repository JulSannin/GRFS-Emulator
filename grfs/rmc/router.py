"""Dispatch: an RMC request goes to a handler named after the method.

A service declares the protocol's id and its name in the schema; the schema turns a method id
into a method name and its parameter list; the handler is the service's own method of that name,
called with the decoded parameters as keyword arguments. So the wire format is never written by
hand, and a service reads as the list of calls it answers.

A request that finds no handler is answered with an RMC error, never with silence. An
unanswered call makes the client reissue it forever - nineteen identical calls to one method in
one capture - and a client that takes a missing reply for a value has been seen to read through
a null pointer and die.
"""

import inspect

from . import message
from .ddl import describe


class RmcError(Exception):
    """Answer this call with an RMC failure."""

    def __init__(self, code: int, note: str = ""):
        super().__init__(note or ("0x%08X" % code))
        self.code = code
        self.note = note


# What a qresult carries when a call worked. Not 0: the top bit marks a failure, and the client
# tests for this exact value in places.
SUCCESS = 0x00010001


class Errors:
    """Codes from the game's own table; facility in the high half, index in the low."""

    CORE_UNKNOWN = 0x80010001
    CORE_ACCESS_DENIED = 0x80010006
    CORE_INVALID_ARGUMENT = 0x8001000A
    CORE_TIMEOUT = 0x8001000B
    RV_CONNECTION_FAILURE = 0x80030001
    RV_NOT_AUTHENTICATED = 0x80030002
    RV_INVALID_USERNAME = 0x80030064
    RV_INVALID_PID = 0x8003006B
    FRIENDS_USER_NOT_FOUND = 0x80100004
    # GameSession, category 0x12 (the table at 55AC90)
    SESSION_UNKNOWN = 0x80120001
    SESSION_INVALID_ARGUMENT = 0x80120002
    SESSION_INVALID_SESSION_KEY = 0x80120003
    SESSION_INVALID_QUERY_ID = 0x80120004
    SESSION_HOST_PRIVILEGE_REQUIRED = 0x80120005
    SESSION_NO_PUBLIC_SLOT = 0x80120006
    SESSION_NO_PRIVATE_SLOT = 0x80120007
    SESSION_HOST_OR_PARTICIPANT_REQUIRED = 0x80120008
    SESSION_PARTICIPANT_NOT_CONNECTED = 0x80120009
    SESSION_NOT_PARTICIPANT = 0x8012000A
    SESSION_ALREADY_PARTICIPANT = 0x8012000B
    SESSION_INVALID_PRINCIPAL_ID = 0x8012000C


class Context:
    """What a handler is given besides its parameters."""

    __slots__ = ("backend", "client", "link", "packet", "service", "method", "server", "args",
                 "call_id")

    def __init__(self, backend, client, link, packet, service, method, server, args, call_id):
        self.backend = backend
        self.client = client
        self.link = link
        self.packet = packet
        self.service = service
        self.method = method
        self.server = server
        self.args = args
        self.call_id = call_id

    def reply_raw(self, body: bytes):
        """Answer with bytes built here, for the few replies whose captured layout does not match
        what the schema declares (see services.auth). Return this from the handler."""
        self.server.send_rmc(self.client, self.link, self.packet,
                             message.response(self.service.protocol_id, self.call_id,
                                              self.method.id, body))
        return RAW

    def encode(self, result: dict) -> bytes:
        """This method's reply as the schema declares it, for appending to by hand."""
        return self.service.schema.encode_response(self.method, result)


class Service:
    """Base for one RMC protocol.

    Most protocols are declared in the DDL the game carries. Ubisoft's Hermes protocols and
    Overlord are not: the game builds those requests by hand, so their layouts were read out of
    the client's own stub functions. A service for one of those declares the layout here, in the
    same shape the DDL would have given it, and the router then treats it like any other:

        schema_classes = [("ClanInfo", [("m_id", "uint32"), ("m_tag", "string")])]
        schema_methods = {13: ("GetCurUserClan", [("out", "uint32", "clanId")], None)}

    A class entry may carry a third element, its parent. A protocol the DDL already declares is
    never replaced.

    A parameter that must never be read - a key the player typed, say - is named in
    secret_params, {method name: (parameter, ...)}: it is skipped on the wire, so it is in no
    log line and no handler ever sees it (see rmc.ddl.Schema.keep_secret).

    The fake game (grfs.fakegame) is given these same declarations, not a copy of them, the way
    the real game has them compiled in.
    """

    protocol_id = 0
    protocol_name = ""
    schema_classes = ()
    schema_methods = None
    secret_params = {}

    def __init__(self, backend):
        self.backend = backend
        self.schema = backend.schema
        from .. import log as logs
        self.log = logs.get(self.protocol_name.replace("Protocol", "") or "service")
        declare(self.schema, self)

    def method_def(self, mid: int):
        return self.schema.method(self.protocol_name, mid)

    def handler(self, name: str):
        fn = getattr(self, name, None)
        return fn if callable(fn) else None

    def handled(self) -> list:
        """(id, name) of every method this service answers, in id order."""
        proto = self.schema.protocols.get(self.protocol_name)
        if proto is None:
            return []
        return sorted((mid, m.name) for mid, m in proto.methods.items()
                      if self.handler(m.name) is not None)


def declare(schema, service) -> None:
    """Put what a service class declares about its protocol into a schema: its classes, its
    methods when the DDL has none for it, and its secret parameters. Takes the class or an
    instance - the server does this for its own schema, the fake game for its copy."""
    for name, fields, *parent in service.schema_classes:
        if name not in schema.classes:
            schema.add_class(name, fields, parent[0] if parent else "")
    if service.schema_methods and service.protocol_name not in schema.protocols:
        schema.add_protocol(service.protocol_name, service.schema_methods)
    for method, params in service.secret_params.items():
        for param in params:
            schema.keep_secret(service.protocol_name, method, param)


class Router:
    def __init__(self, backend):
        self.backend = backend
        self.services = {}
        self.log = backend.log

    def add(self, service: Service) -> Service:
        self.services[service.protocol_id] = service
        return service

    def service_for(self, protocol_id: int):
        return self.services.get(protocol_id)

    def protocol_name(self, protocol_id: int) -> str:
        service = self.services.get(protocol_id)
        if service is not None:
            return service.protocol_name
        return self.backend.protocol_names.get(protocol_id, "protocol 0x%X" % protocol_id)

    def dispatch(self, server, client, link, packet, msg) -> None:
        """Handle one decoded RMC request and send whatever answers it."""
        name = self.protocol_name(msg.protocol)
        service = self.services.get(msg.protocol)
        if service is None:
            self._refuse("%s method %d from %s: no service" % (
                name, msg.method, client.name or client.address), name, msg.method)
            server.send_error(client, link, packet, msg, Errors.CORE_UNKNOWN)
            return

        mdef = service.method_def(msg.method)
        if mdef is None:
            self._refuse("%s method %d from %s: not in the schema" % (
                name, msg.method, client.name or client.address), name, msg.method)
            server.send_error(client, link, packet, msg, Errors.CORE_INVALID_ARGUMENT)
            return

        handler = service.handler(mdef.name)
        if handler is None:
            self._refuse("%s.%s (method %d) from %s: not implemented" % (
                name, mdef.name, msg.method, client.name or client.address), name, msg.method)
            server.send_error(client, link, packet, msg, Errors.CORE_UNKNOWN)
            return

        try:
            args = service.schema.decode_request(mdef, msg.body)
        except Exception as e:
            # Not the bytes when a parameter is secret: they would be the secret.
            self.log.error("%s.%s: cannot decode the request (%s): %s", name, mdef.name, e,
                           "<not shown>" if any(p.secret for p in mdef.params)
                           else msg.body[:64].hex(" "))
            server.send_error(client, link, packet, msg, Errors.CORE_INVALID_ARGUMENT)
            return

        trailing = args.pop("__trailing__", None)
        if trailing:
            self.log.warning("%s.%s: %d bytes past the end of the parameters: %s", name,
                             mdef.name, len(trailing), trailing[:32].hex(" "))
        self.log.info("%s %s.%s call=%d %s", client.name or client.address, name, mdef.name,
                      msg.call_id, describe(args) if args else "()")

        ctx = Context(self.backend, client, link, packet, service, mdef, server, dict(args),
                      msg.call_id)
        try:
            result = _call(handler, ctx, args)
        except RmcError as e:
            self.log.info("%s.%s -> error 0x%08X %s", name, mdef.name, e.code, e.note)
            server.send_error(client, link, packet, msg, e.code)
            return
        except Exception:
            self.log.exception("%s.%s raised", name, mdef.name)
            server.send_error(client, link, packet, msg, Errors.CORE_UNKNOWN)
            return

        if result is RAW:
            return                      # the handler sent its own reply
        try:
            body = service.schema.encode_response(mdef, result or {})
        except Exception:
            self.log.exception("%s.%s: cannot encode %s", name, mdef.name, describe(result))
            server.send_error(client, link, packet, msg, Errors.CORE_UNKNOWN)
            return
        if result:
            self.log.debug("%s.%s -> %s", name, mdef.name, describe(result))
        server.send_rmc(client, link, packet,
                        message.response(msg.protocol, msg.call_id, msg.method, body))


    def _refuse(self, text: str, protocol: str, method: int) -> None:
        """Log a call nothing answers: a warning, unless it is one of the methods left out on
        purpose (services.NOT_ANSWERED), when the reason goes with it instead."""
        from ..services import not_answered
        why = not_answered(protocol, method)
        if why:
            self.log.info("%s - not answered, on purpose: %s", text, why)
        else:
            self.log.warning("%s", text)


# Returned by a handler that has already sent its own reply.
RAW = object()


def _call(handler, ctx, args: dict):
    """Call the handler with the parameters it declares, so a handler may ignore the rest."""
    sig = inspect.signature(handler)
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()):
        return handler(ctx, **args)
    wanted = {name for name, p in sig.parameters.items()
              if p.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)}
    wanted.discard("ctx")
    wanted.discard("self")
    return handler(ctx, **{k: v for k, v in args.items() if k in wanted})
