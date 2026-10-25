"""UbiAccountManagementProtocol (0x1D): turning account ids into names and back.

The friends screen calls 14 LookupUsernamesByUbiAccountIDs immediately after the Uplay
relationships list, handing back the very account ids that list just gave it, because an entry
there carries no name. Until this is answered the tab has nothing to print. Captured, before any
of it was written:

    23 00 00 00 | 9D | 34 01 00 00 | 0E 00 00 00 | 02 00 00 00
      | 0B 00 "TESTONLINE" 00 | 07 00 "NayNik" 00

- protocol 0x1D, call 308, method 14, two strings: the two ids this server had just returned.

A private server has no way to know anybody's real Ubisoft account id - nothing ever tells it one
- so the account name stands in for it. The pid next to it is what the client actually matches
players by, and these maps make the substitution consistent in both directions.
"""

from .. import protocols
from ..rmc.router import Service


class UbiAccounts(Service):
    protocol_id = protocols.UBI_ACCOUNT_MANAGEMENT
    protocol_name = "UbiAccountManagementProtocol"

    def LookupUsernamesByUbiAccountIDs(self, ctx, UbiAccountIds):
        found = {}
        for account_id in UbiAccountIds or []:
            name = self.backend.name_for_ubi_account_id(account_id)
            if name:
                found[account_id] = name
        self.log.info("%s asked about %d account id(s), %d known: %s",
                      ctx.client.name or ctx.client.address, len(UbiAccountIds or []),
                      len(found), ", ".join("%s -> %s" % kv for kv in found.items()) or "-")
        return {"Usernames": found}

    def LookupUbiAccountIDsByUsernames(self, ctx, Usernames):
        return {"UbiAccountIDs": {name: self.backend.ubi_account_id(name)
                                  for name in (Usernames or [])
                                  if self.backend.accounts.get(name)}}

    def LookupUbiAccountIDsByPids(self, ctx, pids):
        out = {}
        for pid in pids or []:
            name = self.backend.accounts.name_for(pid)
            if name:
                out[pid] = self.backend.ubi_account_id(name)
        return {"ubiaccountIDs": out}

    def LookupPrincipalIds(self, ctx, ubiAccountIds):
        out = {}
        for account_id in ubiAccountIds or []:
            account = self.backend.accounts.get(
                self.backend.name_for_ubi_account_id(account_id))
            if account:
                out[account_id] = account.pid
        return {"pids": out}

    def LookupUbiAccountIDsByUsernameSubString(self, ctx, UsernameSubString):
        return {"UbiAccountIDs": {a.name: self.backend.ubi_account_id(a.name)
                                  for a in self.backend.friends.search(UsernameSubString)}}

    def UserHasPlayed(self, ctx, UbiAccountIDs):
        """Everyone in the register has played here at least once - that is how they got in it."""
        return {"UserPresence": {i: bool(self.backend.name_for_ubi_account_id(i))
                                 for i in (UbiAccountIDs or [])}}

    def IsUserPlaying(self, ctx, UbiAccountIDs):
        out = {}
        for account_id in UbiAccountIDs or []:
            account = self.backend.accounts.get(
                self.backend.name_for_ubi_account_id(account_id))
            out[account_id] = bool(account and self.backend.is_online(account.pid))
        return {"UserPresence": out}
