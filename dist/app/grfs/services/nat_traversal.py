"""NATTraversalProtocol (0x03): the relay half of hole punching.

When a game wants to reach another station through NAT, its NAT traversal engine probes the
station itself, and - if the URL is one the RV server handed out, which is what an RVCID in it
means - also asks the server to make the other side probe back:

    1 RequestProbeInitiation(targets)          for prudp:/ targets: "have them probe me"
    3 RequestProbeInitiationExt(targets, url)  for udp:/ targets: "have them probe this URL"

The server's part is to send each target 2 InitiateProbe(url). The two probes cross, each NAT
sees an outgoing packet to the other side first, and the inbound one is let through. Without
the relay only the asking side probes, and that is enough only for an open NAT, a LAN or a
single host.

The client fires both requests and forgets them: the call context is destroyed straight after
sending (8D2530 / 8D2578) and nobody reads the reply, so the answer is plain success whatever
became of the targets. InitiateProbe from the server goes to RVNATRelay::InitiateProbe
(5A2690), which probes the URL without asking the relay again.

The URL a target is told to probe for request 1 is the asker's public one: the address and port
its secure connection reaches us from - the same socket the probes leave from - written into the
station URL it registered, with its RVCID.
"""

from .. import protocols
from ..backend import PLAYER_VPORT
from ..rmc.router import Service


class NatTraversal(Service):
    protocol_id = protocols.NAT_TRAVERSAL
    protocol_name = "NATTraversalProtocol"

    def RequestProbeInitiation(self, ctx, urlTargetList=()):
        self._relay(ctx, urlTargetList, self.public_url(ctx.client))
        return {}

    def RequestProbeInitiationExt(self, ctx, urlTargetList=(), urlStationToProbe=""):
        """The asker names the URL to probe, and it is passed on as given."""
        self._relay(ctx, urlTargetList, urlStationToProbe or self.public_url(ctx.client))
        return {}

    def InitiateProbe(self, ctx, urlStationToProbe=""):
        """A server-to-client call. A client that sends it gets success and nothing else: the
        server has no socket of its own to probe with."""
        self.log.info("%s asked the server itself to probe %s - nothing to do",
                      ctx.client.name or ctx.client.address, urlStationToProbe)
        return {}

    # -----------------------------------------------------------------------------------------
    def public_url(self, client) -> str:
        """The client's station URL as the internet sees it."""
        address, port = client.endpoint
        base = next((u for u in client.urls if u.startswith("prudp:/")), None)
        if base is None:
            return "prudp:/address=%s;port=%d;RVCID=%d" % (address, port, client.rvcid)
        scheme, fields = parse_url(base)
        fields["address"], fields["port"] = address, str(port)
        fields.setdefault("RVCID", str(client.rvcid))
        return scheme + ":/" + ";".join("%s=%s" % kv for kv in fields.items())

    def _relay(self, ctx, targets, url: str) -> None:
        asker = ctx.client
        server = self.backend.servers.get("secure")
        mdef = self.schema.method(self.protocol_name, 2)
        body = self.schema.encode_request(mdef, {"urlStationToProbe": url})
        told, missing, seen = [], [], set()
        for target_url in targets or ():
            target = self.owner_of(target_url)
            if target is asker:
                # Its own URL: there is nobody to tell, and nothing is missing either.
                continue
            if target is None:
                missing.append(target_url)
                continue
            link = target.find_link(self.backend.secure_port, PLAYER_VPORT)
            if server is None or link is None:
                missing.append(target_url)
                continue
            if target.id in seen:
                continue
            seen.add(target.id)
            server.send_request(target, link, protocols.NAT_TRAVERSAL, mdef.id, body)
            told.append(target.name or target.address)
        self.log.info("%s wants probes from %d station(s): %s told to probe %s%s",
                      asker.name or asker.address, len(targets or ()), ", ".join(told) or "nobody",
                      url, "; not here: %s" % ", ".join(missing) if missing else "")

    def owner_of(self, url: str):
        """The connected game a station URL belongs to: by its RVCID, which is ours to give
        out; by PID; and otherwise by address and port, against where each game reaches us from
        and every URL it registered."""
        _, fields = parse_url(url)
        rvcid = _number(fields.get("RVCID"))
        if rvcid:
            found = next((c for c in self.backend.clients if c.rvcid == rvcid), None)
            if found is not None:
                return found
        pid = _number(fields.get("PID"))
        if pid:
            found = self.backend.clients.by_pid(pid)
            if found is not None and found.name:
                return found
        place = (fields.get("address"), _number(fields.get("port")))
        for client in self.backend.clients:
            if not client.name:
                continue
            if place == (client.endpoint[0], client.endpoint[1]):
                return client
            for own in client.urls:
                _, theirs = parse_url(own)
                if place == (theirs.get("address"), _number(theirs.get("port"))):
                    return client
        return None


def parse_url(url: str):
    """'prudp:/address=1.2.3.4;port=5;RVCID=7' -> ('prudp', {'address': ..., ...}), keeping
    the order the fields came in."""
    scheme, _, rest = (url or "").partition(":/")
    fields = {}
    for part in rest.split(";"):
        key, sep, value = part.partition("=")
        if sep:
            fields[key] = value
    return scheme, fields


def _number(text) -> int:
    try:
        return int(text or 0)
    except ValueError:
        return 0
