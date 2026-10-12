"""Process resources used by steps, never serialized into workflow inputs."""

from dataclasses import dataclass

import aiosqlite

from .amenity_client import AmenityClient
from .client import OpenRentClient


@dataclass
class Runtime:
    client: OpenRentClient
    db: aiosqlite.Connection
    quiet: bool = False
    amenity_client: AmenityClient | None = None

    def report(self, message):
        if not self.quiet:
            print(message, flush=True)


_runtime: Runtime | None = None


def configure(runtime: Runtime | None):
    """Install dependencies at startup, before DBOS recovery begins."""
    global _runtime
    _runtime = runtime


def resources() -> Runtime:
    if _runtime is None:
        raise RuntimeError("OpenRent runtime has not been configured")
    return _runtime
