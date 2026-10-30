"""Find the printer's camera once, then remember where it was.

The camera address is the least certain part of the CC1 protocol: 3031 is what the
printer's own web UI uses, but 8080 has been reported on other Centauri models and
the firmware does not advertise the stream anywhere. Making the operator discover
the right port is a bad first-run experience, so probe the configured endpoint
first and fall back to the known alternatives.

A probe is a read-only GET that is closed as soon as the first bytes arrive, so it
costs one request per candidate and nothing on the printer beyond that.
"""

from __future__ import annotations

import logging
import time

import httpx

log = logging.getLogger(__name__)

#: Ports the camera has been reported on, most likely first.
CANDIDATE_PORTS = (3031, 8080)
PROBE_TIMEOUT_S = 3.0
#: Once every candidate has failed, wait this long before probing again. A printer
#: that is switched off would otherwise add probe latency to every snapshot.
RETRY_AFTER_S = 30.0


class CameraEndpoint:
    """Resolves and caches the URL that actually serves an image."""

    def __init__(self, host: str, *, port: int = 3031, path: str = "/video") -> None:
        self.host = host
        self.path = path
        self._preferred_port = port
        self._url: str | None = None
        self._failed_at: float | None = None

    @property
    def configured_url(self) -> str:
        return f"http://{self.host}:{self._preferred_port}{self.path}"

    @property
    def current_url(self) -> str:
        """Last known good URL, or the configured one before the first probe.

        Sync on purpose: the MJPEG broadcaster asks for a URL inside its reconnect
        loop and cannot await a resolution there.
        """
        return self._url or self.configured_url

    def candidates(self) -> list[str]:
        ports = [self._preferred_port]
        ports.extend(port for port in CANDIDATE_PORTS if port != self._preferred_port)
        return [f"http://{self.host}:{port}{self.path}" for port in ports]

    def invalidate(self) -> None:
        """Forget the cached URL so the next resolve probes again."""
        self._url = None

    async def resolve(self) -> str:
        """Return a URL that is known to answer, probing if necessary."""
        if self._url is not None:
            return self._url
        if not self.host:
            return self.configured_url
        if self._failed_at is not None and time.monotonic() - self._failed_at < RETRY_AFTER_S:
            return self.configured_url

        for url in self.candidates():
            if await self._probe(url):
                self._url = url
                self._failed_at = None
                log.info("camera endpoint: %s", url)
                return url

        self._failed_at = time.monotonic()
        log.warning("camera endpoint: nothing answered (tried %s)", ", ".join(self.candidates()))
        return self.configured_url

    async def _probe(self, url: str) -> bool:
        """True if ``url`` answers with image bytes rather than an HTML page."""
        try:
            async with httpx.AsyncClient(timeout=PROBE_TIMEOUT_S) as client:
                async with client.stream("GET", url) as response:
                    if response.status_code != 200:
                        return False
                    content_type = response.headers.get("content-type", "")
                    if "image" not in content_type and "multipart" not in content_type:
                        return False
                    async for chunk in response.aiter_bytes():
                        return bool(chunk)
        except Exception as err:  # noqa: BLE001 - httpx raises several unrelated types
            log.debug("camera probe %s failed: %r", url, err)
        return False
