"""Locate bundled resources separately from persistent configuration and player data."""

from pathlib import Path
import os
import sys


def resource_dir() -> Path:
    return Path(__file__).resolve().parent.parent


def runtime_dir() -> Path:
    # The native Windows launcher runs the signed Python shipped in the distribution.
    # Mutable files belong to the server executable's directory, not app/ or python/.
    configured = os.environ.get("GRFS_RUNTIME_DIR")
    if configured:
        return Path(configured).resolve()
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return resource_dir()
