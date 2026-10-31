"""The three primitives the protocol needs: RC4, Quazal's key derivation and HMAC-MD5."""

import functools
import hashlib
import hmac

# Payload key of every PRUDP packet on the RVSec stream. Public in every Quazal title.
PAYLOAD_KEY = b"CD&ML"


def rc4(key: bytes, data: bytes) -> bytes:
    """RC4 with a fresh key schedule for every call - the game resets it per packet too."""
    s = list(range(256))
    j = 0
    klen = len(key)
    for i in range(256):
        j = (j + s[i] + key[i % klen]) & 0xFF
        s[i], s[j] = s[j], s[i]
    out = bytearray(len(data))
    i = j = 0
    for n, b in enumerate(data):
        i = (i + 1) & 0xFF
        j = (j + s[i]) & 0xFF
        s[i], s[j] = s[j], s[i]
        out[n] = b ^ s[(s[i] + s[j]) & 0xFF]
    return bytes(out)


@functools.lru_cache(maxsize=256)
def derive_key(pid: int, password: bytes) -> bytes:
    """Kerberos user key: MD5 applied 65000 + pid % 1024 times to the password.

    The game never uses a real password here: ordinary players log in with the fixed
    "UbiDummyPwd", the telemetry connection with its own fixed one.
    """
    buf = password
    for _ in range(65000 + pid % 1024):
        buf = hashlib.md5(buf).digest()
    return buf


def hmac_md5(key: bytes, data: bytes) -> bytes:
    return hmac.new(key, data, hashlib.md5).digest()
