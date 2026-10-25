"""PrivilegesProtocol (0x23): what this account is allowed to use.

The client calls 6 GetPrivilegesEx and 5 GetExpiredPrivileges in the burst right after logging
in, before the menu is drawn. The list says which weapons, heads, camouflages and maps are
unlocked - including the one that matters most, "allow to play online".

The ids and their descriptions come from the previous implementation, which read them out of the
game's executable. They are not in the schema: PrivilegeEx carries an id, a description and a
duration, and nothing says what any particular id means.

The reply is also the first one long enough to be split across PRUDP fragments, which is how the
fragment numbering bug in the previous attempt was found - from a player's second login onward
the reply was numbered past the start of the client's receive window and never arrived at all.

ActivateKey and ActivateKeyWithExpectedPrivileges are how the game redeems a code typed into its
"enter a key" screen. There are no keys here to redeem - every privilege is already handed out -
so both refuse with the game's own Privilege InvalidKey (0x800B0004). That is the answer the
client is built for: its callback (A4B640) turns an error in the Privilege facility into the
on-screen message for that code, and any other error into a generic failure.

The key itself is something this server must never write down, and the router logs every
request's parameters before the handler sees them. So uniqueKey is a secret parameter
(Service.secret_params): skipped on the wire instead of decoded, so it is never in memory past
the packet, never in a log line and never on disk.
"""

from .. import protocols
from ..rmc.router import RmcError, Service

# Privilege facility in the game's error table.
INVALID_KEY = 0x800B0004

FOREVER = -1


def _privileges() -> list:
    rows = [(1, "Allow to play online")]
    for base, who in ((0x3E8, "Engineer"), (0x3F0, "Rifleman"), (0x3F8, "Scout")):
        rows.extend((base + n, "%s Head Variant %d" % (who, n + 1)) for n in range(8))
    rows.extend([
        (0x401, "Weapon AK47"),
        (0x404, "Weapon M40A5"),
        (0x405, "Weapon MK14"),
        (0x406, "Weapon MN913"),
        (0x410, "Map MH01 Tramway"),
        (0x422, "Tiger Stripe Camo"),
        (0x453, "Winter Digital Camo"),
        (0x13BA, "R6 weapon skin, for a GRAW 1/2 or Splinter Cell: Conviction save"),
        (0x13C4, "Tom Clancy weapon skin, for a GRAW 1/2 or Conviction save"),
        (0x13CE, "Splinter Cell weapon skin, for a Conviction save"),
        (0x13D6, "Unidentified privilege found in the game's executable"),
        # What the four Uplay rewards unlock (services.uplay). The client asks Misc
        # UnlockPrivilegeTrial for these only when they are missing from this list.
        (0x1432, "Uplay reward 1"),
        (0x143C, "Uplay reward 2"),
        (0x1446, "Uplay reward 3"),
        (0x1450, "Uplay reward 4"),
    ])
    return [{"m_ID": pid, "m_description": text, "m_duration": FOREVER} for pid, text in rows]


PRIVILEGES = _privileges()


class Privileges(Service):
    protocol_id = protocols.PRIVILEGES
    protocol_name = "PrivilegesProtocol"

    secret_params = {"ActivateKey": ("uniqueKey",),
                     "ActivateKeyWithExpectedPrivileges": ("uniqueKey",)}

    def GetPrivilegesEx(self, ctx, localeCode=""):
        self.log.debug("%s: %d privileges (locale %s)", ctx.client.name or ctx.client.address,
                       len(PRIVILEGES), localeCode or "-")
        return {"privilegesEx": PRIVILEGES}

    def GetPrivileges(self, ctx, localeCode=""):
        """The older shape of the same question: a map of id to privilege, without durations."""
        return {"privileges": [(p["m_ID"], {"m_ID": p["m_ID"],
                                            "m_description": p["m_description"]})
                               for p in PRIVILEGES]}

    def GetExpiredPrivileges(self, ctx):
        """Nothing here expires."""
        return {"expiredPrivileges": []}

    def GetPrivilegeRemainDuration(self, ctx, privilegeID):
        return {"seconds": FOREVER}

    def ActivateKey(self, ctx, languageCode=""):
        self.log.info("%s tried to activate a key - there are none here",
                      ctx.client.name or ctx.client.address)
        raise RmcError(INVALID_KEY, "no keys here")

    def ActivateKeyWithExpectedPrivileges(self, ctx, languageCode="", expectedPrivilegeIDs=None):
        self.log.info("%s tried to activate a key for privilege(s) %s - there are none here",
                      ctx.client.name or ctx.client.address,
                      ", ".join("0x%X" % i for i in expectedPrivilegeIDs or []) or "-")
        raise RmcError(INVALID_KEY, "no keys here")
