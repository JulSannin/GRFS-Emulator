"""LocalizationProtocol (0x27): which language a player's game is in.

The Overlord sign-in job (JobSetLocalization, inside JobOverlordPostLogin) sends SetLocaleCode
with the game's language, and an error there ends the job before the rest of the post-login
steps - so it is always accepted. GetLocaleCode has no caller in this client; it answers what the
player last set, or "en-US" for one who never did.

Nothing on this server is translated, so the code is only remembered, per pid, for as long as the
server runs: the game sets it again on every sign-in.
"""

from .. import protocols
from ..rmc.router import Service

DEFAULT = "en-US"


class Localization(Service):
    protocol_id = protocols.LOCALIZATION
    protocol_name = "LocalizationProtocol"

    def __init__(self, backend):
        super().__init__(backend)
        self.codes = {}

    def GetLocaleCode(self, ctx):
        return {"localCode": self.codes.get(ctx.client.pid, DEFAULT)}

    def SetLocaleCode(self, ctx, localCode=""):
        if localCode and ctx.client.pid:
            self.codes[ctx.client.pid] = localCode
        self.log.debug("%s speaks %s", ctx.client.name or ctx.client.address, localCode or "-")
