"""The Kerberos-style ticket the authentication server hands out.

Contents, before encryption:

    session key (16 bytes) | server pid (u32) | length (u32) | opaque ticket bytes

encrypted with RC4 under a key derived from the *user's* pid and password, with an HMAC-MD5 of
the ciphertext appended. The client decrypts it, keeps the session key, and passes the opaque
part on to the secure server inside its CONNECT packet - it never looks inside, so what is in
there only has to be the same bytes in both places.

Two passwords exist in this game, both constants: ordinary players authenticate with
"UbiDummyPwd" (the real Ubisoft password goes to Ubisoft, not here), and the telemetry
connection - which logs in as the user "Tracking" - with "JaDe!". Both strings are in the game's
own executable.
"""

import os
from dataclasses import dataclass

from .prudp.crypto import derive_key, hmac_md5, rc4
from .rmc.stream import Writer

TRACKING_NAME = "Tracking"
TRACKING_PID = 0x1111
SERVER_PID = 0x1000

PASSWORD_DEFAULT = b"UbiDummyPwd"
PASSWORD_TRACKING = b"JaDe!"


def password_for(name: str) -> bytes:
    return PASSWORD_TRACKING if name == TRACKING_NAME else PASSWORD_DEFAULT


@dataclass
class Ticket:
    user_pid: int
    server_pid: int
    session_key: bytes
    blob: bytes

    def encode(self, password: bytes) -> bytes:
        plain = (Writer().raw(self.session_key).u32(self.server_pid)
                 .u32(len(self.blob)).raw(self.blob).bytes())
        key = derive_key(self.user_pid, password)
        body = rc4(key, plain)
        return body + hmac_md5(key, body)


def new_session_key() -> bytes:
    return os.urandom(16)


def new_blob() -> bytes:
    """The opaque half. The client carries it to the secure server without reading it."""
    return os.urandom(36)
