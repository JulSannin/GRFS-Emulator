"""Tests. Run them all with `python -m unittest discover` from the project root."""

import logging

# Tests that do not set up logging would otherwise have the server's warnings printed through
# logging's last-resort handler, in the middle of the dots. assertLogs and the harness still
# see every record.
logging.getLogger("grfs").addHandler(logging.NullHandler())
