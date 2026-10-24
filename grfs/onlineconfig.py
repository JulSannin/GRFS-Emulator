"""The configuration service the game asks before it touches anything else.

    GET /OnlineConfigService.svc/GetOnlineConfig?onlineConfigID=<key>&target=client
    Host: onlineconfigservice.ubi.com

The answer is a JSON list of name/value pairs. Only one of them matters to us: SandboxUrl, the
address of the authentication server. The game reaches this service because the machine's hosts
file points onlineconfigservice.ubi.com at the server.

Ubisoft's own service is still up and still answers this request - it hands out
"prudp:/address=ne1-z3-as-rdv03.ubisoftcom;port=21220", a host name with no dot before "com"
that has never resolved. The rest of the values below are the ones it really returns, kept
because they are well-formed: the hosts are long dead, so the client fails cleanly on them
instead of parsing something it did not expect.

The address in SandboxUrl is worked out per request from who is asking, for the same reason the
login does it that way: a game on this machine needs 127.0.0.1 and a game on the network needs
the address that reaches us.
"""

import asyncio
import json

from . import log as logs

PATH = "/OnlineConfigService.svc/GetOnlineConfig"

# The id the game sends. Its own executable carries it, and it is the same one Ghost Recon
# Online used. Requests for anything else are answered all the same - there is nothing to gain
# from being strict - but the difference is worth a line in the log.
CONFIG_ID = "23ad683803a0457cabce83f905811dbc"

OTHERS = [
    ("SandboxUrlWS", "ne1-z3-as-rdv06.ubisoft.com:21215"),
    ("uplay_DownloadServiceUrl",
     "https://secure.ubi.com/UplayServices/UplayFacade/DownloadServicesRESTXML.svc/REST/XML/?url="),
    ("uplay_DynContentBaseUrl", "http://static8.cdn.ubi.com/u/Uplay/"),
    ("uplay_DynContentSecureBaseUrl", "http://static8.cdn.ubi.com/"),
    ("uplay_LinkappBaseUrl", "http://static8.cdn.ubi.com/u/Uplay/Packages/linkapp/1.1/"),
    ("uplay_PackageBaseUrl", "http://static8.cdn.ubi.com/u/Uplay/Packages/1.0.1/"),
    ("uplay_WebServiceBaseUrl",
     "https://secure.ubi.com/UplayServices/UplayFacade/ProfileServicesFacadeRESTXML.svc/REST/"),
]


class OnlineConfigServer:
    def __init__(self, backend):
        self.backend = backend
        self.log = logs.get("onlineconfig")
        self.port = backend.config.int("onlineconfig_port", 80)
        self.served = 0
        self.server = None

    async def start(self):
        bind = self.backend.config.get("bind", "0.0.0.0")
        self.server = await asyncio.start_server(self.handle, bind, self.port)
        self.log.info("listening on %s:%d%s", bind, self.port, PATH)
        return self.server

    def close(self):
        if self.server is not None:
            self.server.close()

    # ----- HTTP -----------------------------------------------------------------------------
    async def handle(self, reader, writer):
        peer = writer.get_extra_info("peername") or ("?", 0)
        try:
            line = await asyncio.wait_for(reader.readline(), 15)
            if not line:
                return
            parts = line.decode("latin-1").split()
            method, path = (parts + ["", ""])[:2]
            while True:                                  # headers, which we do not need
                header = await asyncio.wait_for(reader.readline(), 15)
                if header in (b"\r\n", b"\n", b""):
                    break
            if method != "GET" or not path.startswith(PATH):
                self.log.info("%s asked for %s %s - not ours", peer[0], method, path)
                writer.write(self.reply(404, b"not found", "text/plain"))
                await writer.drain()
                return
            if CONFIG_ID not in path:
                self.log.info("%s asked for a configuration id we do not know: %s", peer[0], path)
            body = self.config_for(peer[0]).encode("utf-8")
            writer.write(self.reply(200, body, "application/json; charset=utf-8"))
            await writer.drain()
            self.served += 1
            self.log.info("%s got the configuration (%d bytes, %d served)", peer[0], len(body),
                          self.served)
        except asyncio.TimeoutError:
            self.log.debug("%s opened a connection and said nothing", peer[0])
        except (ConnectionError, OSError) as e:
            self.log.debug("%s: %s", peer[0], e)
        finally:
            try:
                writer.close()
            except OSError:
                pass

    @staticmethod
    def reply(status: int, body: bytes, content_type: str) -> bytes:
        reason = {200: "OK", 404: "Not Found"}.get(status, "OK")
        # Headers as the live service sent them. Nothing depends on them, but a capture taken
        # here then looks like a capture taken there.
        head = ("HTTP/1.1 %d %s\r\n"
                "Cache-Control: private\r\n"
                "Content-Length: %d\r\n"
                "Content-Type: %s\r\n"
                "Server: Microsoft-IIS/7.5\r\n"
                "X-AspNet-Version: 2.0.50727\r\n"
                "X-Powered-By: ASP.NET\r\n"
                "Connection: close\r\n\r\n" % (status, reason, len(body), content_type))
        return head.encode("latin-1") + body

    # ----- the configuration itself ---------------------------------------------------------
    def config_for(self, address: str) -> str:
        advertised = self.backend.address_for(address)
        values = [("SandboxUrl", "prudp:/address=%s;port=%d" % (advertised,
                                                                self.backend.auth_port))]
        values.extend(OTHERS)
        extra = self.backend.config.get("punch_detect_urls")
        if extra:
            # Unverified, and off unless asked for. The game has a module of its own that works
            # out the NAT type from servers named by this key, and its screens can refuse to join
            # ("Unable to join due to STRICT NAT"). Ubisoft's own configuration does not carry
            # the key at all, so whether the game ever uses it here is exactly what setting this
            # is for: point it at something that logs and watch whether anything arrives.
            values.append(("punch_DetectUrls", extra))
            self.log.info("offering punch_DetectUrls=%s (an experiment - see the comment)", extra)
        body = json.dumps([{"Name": name, "Values": [value]} for name, value in values])
        # The live service escapes the slashes in its URLs. It makes no difference to a JSON
        # reader, and it keeps the bytes identical to a capture of the real thing.
        return body.replace("/", "\\/")
