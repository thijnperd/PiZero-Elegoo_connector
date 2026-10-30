"""SDCP v3 client for the Elegoo Centauri Carbon (CC1).

Vendored from `pycentauri <https://github.com/bjan/pycentauri>`_ (Apache-2.0),
CC1 subset only: protocol envelope, wire models, UDP discovery, chunked upload,
MJPEG fan-out and the WebSocket client. See NOTICE for the attribution and the
list of changes.

The package was renamed from ``pycentauri`` to ``cc1client`` so it cannot collide
with an installed copy of the upstream library. Nothing else was rewritten.
"""

from cc1client.client import (
    ControlDisabledError,
    Printer,
    PrinterError,
    RequestTimeoutError,
)
from cc1client.discovery import DiscoveredPrinter, discover
from cc1client.models import Attributes, PrintStatus, Status

__all__ = [
    "Attributes",
    "ControlDisabledError",
    "DiscoveredPrinter",
    "PrintStatus",
    "Printer",
    "PrinterError",
    "RequestTimeoutError",
    "Status",
    "discover",
]
