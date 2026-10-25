"""TitleStorageProtocol (0x33): files the publisher could push to every copy of the game.

The client asks once, after sign-in, for content of type "GameConfigContent" (BrowseContentsByType
with every flag mask open). On the live servers that could replace DLC\\dlc\\dlc.big; with nothing
found the client logs "No remote game config file found." and keeps the copy on disk, which is
the right thing for a game whose DLC is all installed already. So there is no content here: both
browse calls answer an empty list, and asking about or downloading a content id fails with the
game's own TitleStorage InvalidContent, since no id names anything.
"""

from .. import protocols
from ..rmc.router import RmcError, Service

# TitleStorage's facility in the game's error table.
INVALID_CONTENT = 0x80150004


class TitleStorage(Service):
    protocol_id = protocols.TITLE_STORAGE
    protocol_name = "TitleStorageProtocol"

    def BrowseContents(self, ctx, flags=0, flagsMaskType=0, extendedFlags=0,
                       extendedFlagsMaskType=0):
        return {"contents": []}

    def BrowseContentsByType(self, ctx, contentType="", flags=0, flagsMaskType=0,
                             extendedFlags=0, extendedFlagsMaskType=0):
        self.log.debug("%s: no %s here", ctx.client.name or ctx.client.address,
                       contentType or "content")
        return {"contents": []}

    def GetContentDetails(self, ctx, contentID=0):
        raise RmcError(INVALID_CONTENT, "no content %d" % contentID)

    def DownloadContent(self, ctx, contentID=0):
        raise RmcError(INVALID_CONTENT, "no content %d" % contentID)
