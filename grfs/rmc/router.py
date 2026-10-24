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
    SESSION_UNKNOWN = 0x80120001
    SESSION_INVALID_ARGUMENT = 0x80120002
    SESSION_INVALID_SESSION_KEY = 0x80120003
    SESSION_NO_PUBLIC_SLOT = 0x80120006
    SESSION_NOT_PARTICIPANT = 0x8012000A


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
    """Base for one RMC protocol."""

    protocol_id = 0
    protocol_name = ""

    def __init__(self, backend):
        self.backend = backend
        self.schema = backend.schema
        from .. import log as logs
        self.log = logs.get(self.protocol_name.replace("Protocol", "") or "service")

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
            self.log.warning("%s method %d from %s: no service", name, msg.method,
                             client.name or client.address)
            server.send_error(client, link, packet, msg, Errors.CORE_UNKNOWN)
            return

        mdef = service.method_def(msg.method)
        if mdef is None:
            self.log.warning("%s method %d from %s: not in the schema", name, msg.method,
                             client.name or client.address)
            server.send_error(client, link, packet, msg, Errors.CORE_INVALID_ARGUMENT)
            return

        handler = service.handler(mdef.name)
        if handler is None:
            self.log.warning("%s.%s (method %d) from %s: not implemented", name, mdef.name,
                             msg.method, client.name or client.address)
            server.send_error(client, link, packet, msg, Errors.CORE_UNKNOWN)
            return

        try:
            args = service.schema.decode_request(mdef, msg.body)
        except Exception as e:
            self.log.error("%s.%s: cannot decode the request (%s): %s", name, mdef.name, e,
                           msg.body[:64].hex(" "))
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
