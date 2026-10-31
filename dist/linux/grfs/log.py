"""Logging.

Deliberately plain: stdlib logging, one line per event, console plus a rotating file. The
previous attempt put the log in a GUI text box and spent four crashes and several evenings of
"random" hangs on it - the window could not keep up with the packet rate, network threads blocked
on it, and the client went into retransmission storms. Nothing in the hot path may do more than
format a string and hand it to a queue, which is what logging's handlers already do.

Credentials never reach here: the fields that carry them are skipped while decoding, not masked
afterwards (see rmc.ddl.SENSITIVE).
"""

import logging
import logging.handlers
import os

from .paths import runtime_dir

LEVELS = {"debug": logging.DEBUG, "info": logging.INFO, "warning": logging.WARNING,
          "error": logging.ERROR}

# Everything the packet layer logs per datagram sits below INFO, so a normal run stays readable
# and a hunt is one setting away.
TRACE = logging.DEBUG - 5
logging.addLevelName(TRACE, "TRACE")


def trace(self, message, *args, **kwargs):
    if self.isEnabledFor(TRACE):
        self._log(TRACE, message, args, **kwargs)


logging.Logger.trace = trace


class RingHandler(logging.Handler):
    """The last few hundred lines, for the status page to show.

    A list with a cap, nothing more: the page asks for them, the handler never pushes anywhere,
    and formatting happens here rather than in whatever is reading.
    """

    def __init__(self, capacity: int = 400):
        super().__init__()
        self.capacity = capacity
        self.lines = []

    def emit(self, record):
        try:
            self.lines.append({
                "time": record.created,
                "level": record.levelname,
                "source": record.name.replace("grfs.", ""),
                "text": record.getMessage(),
            })
        except Exception:
            return
        if len(self.lines) > self.capacity:
            del self.lines[:len(self.lines) - self.capacity]

    def recent(self, limit: int = 200) -> list:
        return self.lines[-limit:]


ring = RingHandler()


def setup(level: str = "info", path: str = None) -> logging.Logger:
    root = logging.getLogger("grfs")
    root.handlers.clear()
    root.setLevel(TRACE if level.lower() == "trace" else LEVELS.get(level.lower(), logging.INFO))
    fmt = logging.Formatter("%(asctime)s %(levelname)-5s %(name)-18s %(message)s",
                            "%H:%M:%S")
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.addHandler(console)
    root.addHandler(ring)
    if path:
        full = path if os.path.isabs(path) else os.path.join(runtime_dir(), path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        # The previous run's log is kept rather than truncated: a crash is usually diagnosed from
        # the run before the restart.
        handler = logging.handlers.RotatingFileHandler(full, maxBytes=8 << 20, backupCount=5,
                                                       encoding="utf-8")
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)-5s %(name)-18s %(message)s", "%Y-%m-%d %H:%M:%S"))
        root.addHandler(handler)
    root.propagate = False
    return root


def get(name: str) -> logging.Logger:
    return logging.getLogger("grfs." + name)


def hexs(data: bytes, limit: int = 48) -> str:
    head = data[:limit].hex(" ").upper()
    return head + (" ...(%d)" % len(data) if len(data) > limit else "")
