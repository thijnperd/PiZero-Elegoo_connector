"""Owns the single WebSocket connection to the printer.

This is principle P3 in docs/DESIGN_PHILOSOPHY.md: one connection owner per printer,
many viewers. The CC1 accepts about five WebSocket clients and closes idle ones after
roughly a minute, so nothing else in this project is allowed to open one.

Responsibilities:

* reconnect forever with exponential backoff (the printer reboots, WiFi drops)
* learn and cache the ``MainboardID`` (every SDCP command needs it, and the firmware
  does not push Attributes while paused or errored)
* feed status frames into the :class:`StateStore`
* fall back to ``Cmd 0`` polling — the firmware's push scheduler can wedge at idle and
  ``Cmd 512`` is still acknowledged, so "subscribed" does not mean "receiving"
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from pathlib import Path
from typing import Any

from cc1client.client import Printer, PrinterError
from cc1client.discovery import discover

from .config import Config
from .state import StateStore

log = logging.getLogger(__name__)

BACKOFF_START_S = 2.0
BACKOFF_MAX_S = 30.0
ATTR_TIMEOUT_S = 8.0


class Supervisor:
    def __init__(self, config: Config, store: StateStore) -> None:
        self.config = config
        self.store = store
        self.printer: Printer | None = None
        self._task: asyncio.Task[None] | None = None
        self._cache = self._read_cache()

    # --- cache -------------------------------------------------------------

    def _read_cache(self) -> dict[str, Any]:
        try:
            raw = json.loads(Path(self.config.cache_file).read_text("utf-8"))
            return raw if isinstance(raw, dict) else {}
        except (OSError, ValueError):
            return {}

    def _write_cache(self) -> None:
        data = {
            "mainboard_id": self._cache.get("mainboard_id"),
            "firmware": self._cache.get("firmware"),
            "printer_host": self.config.printer_host,
        }
        try:
            Path(self.config.state_dir).mkdir(parents=True, exist_ok=True)
            Path(self.config.cache_file).write_text(json.dumps(data, indent=2), "utf-8")
        except OSError as err:  # a read-only FS must not stop printing
            log.warning("could not cache printer identity: %r", err)

    # --- identity ----------------------------------------------------------

    async def resolve_mainboard_id(self) -> str | None:
        """Prefer configuration, then the cache, then a UDP broadcast."""
        if self.config.mainboard_id:
            return self.config.mainboard_id
        cached = self._cache.get("mainboard_id")
        if cached:
            return str(cached)
        try:
            found = await discover(timeout=3.0)
        except OSError as err:
            log.warning("UDP discovery failed: %r", err)
            return None
        for printer in found:
            if not self.config.printer_host or printer.host == self.config.printer_host:
                if printer.mainboard_id:
                    self._cache["mainboard_id"] = printer.mainboard_id
                    self._cache["firmware"] = printer.firmware_version
                    self._write_cache()
                    return printer.mainboard_id
        return None

    # --- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="printer-supervisor")

    async def close(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
        if self.printer is not None:
            with contextlib.suppress(Exception):
                await self.printer.close()
            self.printer = None

    async def _run(self) -> None:
        if not self.config.printer_host:
            self.store.set_connection("offline", error="EC_PRINTER_HOST is not set")
            self.store.log("No printer configured (set EC_PRINTER_HOST)", level="error", kind="config")
            return

        backoff = BACKOFF_START_S
        while True:
            self.store.set_connection("connecting")
            try:
                mainboard_id = await self.resolve_mainboard_id()
                if not mainboard_id:
                    raise PrinterError(
                        "could not determine MainboardID (UDP discovery found nothing). "
                        "Set EC_MAINBOARD_ID once; it is printed in the log on a successful connect."
                    )
                printer = await Printer.connect(
                    self.config.printer_host,
                    enable_control=self.config.control,
                    push_period_ms=self.config.push_ms,
                    mainboard_id=mainboard_id,
                )
                self.printer = printer
                self.store.set_connection("online")
                backoff = BACKOFF_START_S
                attrs_task = asyncio.create_task(self._refresh_attributes(printer))
                try:
                    async for status in printer.watch():
                        self.store.set_status(status)
                finally:
                    attrs_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await attrs_task
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001 - any failure means "retry"
                message = str(err) or err.__class__.__name__
                log.warning("printer link down: %s", message)
                self.store.set_connection("offline", error=message)
            finally:
                if self.printer is not None:
                    with contextlib.suppress(Exception):
                        await self.printer.close()
                    self.printer = None

            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, BACKOFF_MAX_S)

    async def _refresh_attributes(self, printer: Printer) -> None:
        """Fetch identity once per connection. Never fatal: a paused printer is silent."""
        try:
            attributes = await asyncio.wait_for(printer.attributes(), timeout=ATTR_TIMEOUT_S)
        except (asyncio.TimeoutError, PrinterError, Exception) as err:  # noqa: BLE001
            log.info("attributes unavailable: %r", err)
            self.store.set_attributes(_EmptyAttributes(), printer.mainboard_id)
            return
        self.store.set_attributes(attributes, printer.mainboard_id)
        if attributes.mainboard_id or printer.mainboard_id:
            self._cache["mainboard_id"] = attributes.mainboard_id or printer.mainboard_id
        if attributes.firmware_version:
            self._cache["firmware"] = attributes.firmware_version
        self._write_cache()


class _EmptyAttributes:
    """Stand-in when the firmware won't answer, so the MainboardID still lands."""

    mainboard_id: str | None = None
    firmware_version: str | None = None
