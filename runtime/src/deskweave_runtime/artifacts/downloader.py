"""Hardened image downloader.

Search results are untrusted input: a query can return a URL pointing at
``127.0.0.1``, at a cloud metadata endpoint, at a 4 GB file, or at an SVG with
script inside. This module is the single place that turns such a URL into a file
on disk, and it enforces:

* **Scheme / host policy** — only ``http``/``https``, default ports, no userinfo,
  and no loopback / private / link-local / reserved addresses (SSRF guard).
* **Redirects** — followed manually (max 3) and re-validated at every hop, so a
  public URL cannot bounce to an internal one.
* **Size and time** — a byte cap and an overall deadline while streaming.
* **Content sniffing** — the media type is derived from magic bytes, never from
  the ``Content-Type`` header; SVG (and anything unrecognised) is rejected.
* **Deterministic paths** — files are named from the artifact id, never from
  remote input, so path traversal is impossible by construction.

The downloader is synchronous internally (``http.client``) and exposed through an
async method that runs it on a worker thread.
"""

from __future__ import annotations

import asyncio
import hashlib
import http.client
import ipaddress
import os
import socket
import ssl
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from deskweave_contracts import UnsafeResourceError

DEFAULT_MAX_BYTES = 8 * 1024 * 1024
DEFAULT_TIMEOUT = 20.0
DEFAULT_MAX_REDIRECTS = 3
_ALLOWED_PORTS = frozenset({80, 443})
_REDIRECT_CODES = frozenset({301, 302, 303, 307, 308})
_CHUNK = 64 * 1024
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


@dataclass(frozen=True)
class DownloadedImage:
    """Result of a successful, validated download."""

    path: Path
    media_type: str
    extension: str
    size_bytes: int
    sha256: str
    width: int | None
    height: int | None


def _is_public_ip(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    return not (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    )


def validate_remote_url(url: str, *, max_redirects: int = DEFAULT_MAX_REDIRECTS) -> None:
    """Reject URLs that must never be fetched.

    Raises :class:`~deskweave_contracts.UnsafeResourceError` with a diagnostic
    reason. This is the default guard used by :class:`ImageDownloader`.
    """
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"}:
        raise UnsafeResourceError(
            f"unsupported URL scheme {parts.scheme!r}; only http and https are allowed",
            details={"url": url},
        )
    if parts.username or parts.password:
        raise UnsafeResourceError(
            "URLs with embedded credentials are not allowed",
            details={"url": url},
        )
    host = parts.hostname
    if not host:
        raise UnsafeResourceError("URL has no host", details={"url": url})
    port = parts.port or (443 if parts.scheme == "https" else 80)
    if port not in _ALLOWED_PORTS:
        raise UnsafeResourceError(
            f"port {port} is not allowed; only 80 and 443 are permitted",
            details={"url": url},
        )

    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise UnsafeResourceError(
            f"could not resolve host {host!r}: {exc}", details={"url": url}
        ) from exc
    addresses = {info[4][0] for info in infos}
    if not addresses:
        raise UnsafeResourceError(f"host {host!r} resolved to no addresses", details={"url": url})
    for address in addresses:
        if not _is_public_ip(address):
            raise UnsafeResourceError(
                f"host {host!r} resolves to a non-public address ({address}); refusing to fetch",
                details={"url": url, "address": address},
            )


def sniff_image(data: bytes) -> tuple[str, str, int | None, int | None]:
    """Identify an image from its magic bytes.

    Returns ``(media_type, extension, width, height)``. Raises
    :class:`~deskweave_contracts.UnsafeResourceError` for anything that is not a
    supported raster image — including SVG, which can carry script.
    """
    if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
        width = int.from_bytes(data[16:20], "big")
        height = int.from_bytes(data[20:24], "big")
        return "image/png", ".png", width, height
    if data.startswith(b"\xff\xd8\xff"):
        width, height = _jpeg_size(data)
        return "image/jpeg", ".jpg", width, height
    if data.startswith(b"GIF87a") or data.startswith(b"GIF89a"):
        if len(data) >= 10:
            return (
                "image/gif",
                ".gif",
                int.from_bytes(data[6:8], "little"),
                int.from_bytes(data[8:10], "little"),
            )
        return "image/gif", ".gif", None, None
    if data.startswith(b"BM") and len(data) >= 26:
        return (
            "image/bmp",
            ".bmp",
            int.from_bytes(data[18:22], "little"),
            int.from_bytes(data[22:26], "little"),
        )
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        width, height = _webp_size(data)
        return "image/webp", ".webp", width, height
    snippet = data[:64].lstrip()
    if snippet.startswith((b"<svg", b"<?xml")):
        raise UnsafeResourceError(
            "SVG images are not accepted: they can carry executable script",
            details={"detected": "svg"},
        )
    raise UnsafeResourceError(
        "response is not a supported image (png, jpeg, gif, webp, bmp)",
        details={"magic": data[:12].hex()},
    )


def _jpeg_size(data: bytes) -> tuple[int | None, int | None]:
    index = 2
    length = len(data)
    while index + 9 < length:
        if data[index] != 0xFF:
            index += 1
            continue
        marker = data[index + 1]
        if marker in {0xD8, 0xD9}:
            index += 2
            continue
        if index + 4 > length:
            return None, None
        segment_length = int.from_bytes(data[index + 2 : index + 4], "big")
        if marker in {
            0xC0,
            0xC1,
            0xC2,
            0xC3,
            0xC5,
            0xC6,
            0xC7,
            0xC9,
            0xCA,
            0xCB,
            0xCD,
            0xCE,
            0xCF,
        }:
            if index + 9 <= length:
                height = int.from_bytes(data[index + 5 : index + 7], "big")
                width = int.from_bytes(data[index + 7 : index + 9], "big")
                return width, height
            return None, None
        if segment_length < 2:
            return None, None
        index += 2 + segment_length
    return None, None


def _webp_size(data: bytes) -> tuple[int | None, int | None]:
    if len(data) < 30:
        return None, None
    chunk = data[12:16]
    if chunk == b"VP8X":
        width = int.from_bytes(data[24:27], "little") + 1
        height = int.from_bytes(data[27:30], "little") + 1
        return width, height
    if chunk == b"VP8 ":
        width = int.from_bytes(data[26:28], "little") & 0x3FFF
        height = int.from_bytes(data[28:30], "little") & 0x3FFF
        return width, height
    if chunk == b"VP8L":
        bits = int.from_bytes(data[21:25], "little")
        width = (bits & 0x3FFF) + 1
        height = ((bits >> 14) & 0x3FFF) + 1
        return width, height
    return None, None


class ImageDownloader:
    """Downloads remote images into a directory, enforcing safety limits."""

    def __init__(
        self,
        root: Path,
        *,
        max_bytes: int = DEFAULT_MAX_BYTES,
        timeout: float = DEFAULT_TIMEOUT,
        max_redirects: int = DEFAULT_MAX_REDIRECTS,
        url_guard: Callable[[str], None] | None = None,
    ) -> None:
        self._root = Path(root)
        self._max_bytes = max_bytes
        self._timeout = timeout
        self._max_redirects = max_redirects
        self._guard = url_guard or validate_remote_url

    async def download(self, url: str, *, artifact_id: str) -> DownloadedImage:
        """Fetch ``url`` and store it as ``<root>/<artifact_id>.<ext>``."""
        return await asyncio.to_thread(self._download_sync, url, artifact_id)

    def _download_sync(self, url: str, artifact_id: str) -> DownloadedImage:
        deadline = time.monotonic() + self._timeout
        current = url
        for _ in range(self._max_redirects + 1):
            self._guard(current)
            status, headers, body = self._fetch(current, deadline)
            if status in _REDIRECT_CODES:
                location = headers.get("location")
                if not location:
                    raise UnsafeResourceError(
                        "redirect response without a Location header",
                        details={"url": current, "status": status},
                    )
                current = _absolute_location(current, location)
                continue
            if status != 200:
                raise UnsafeResourceError(
                    f"remote server returned HTTP {status}",
                    details={"url": current, "status": status},
                )
            break
        else:
            raise UnsafeResourceError(
                f"too many redirects (limit {self._max_redirects})", details={"url": url}
            )

        media_type, extension, width, height = sniff_image(body)
        digest = hashlib.sha256(body).hexdigest()

        self._root.mkdir(parents=True, exist_ok=True)
        target = self._root / f"{artifact_id}{extension}"
        resolved_root = self._root.resolve()
        resolved_target = target.resolve()
        if resolved_root not in resolved_target.parents:
            raise UnsafeResourceError(
                "refusing to write outside the artifact root",
                details={"target": str(resolved_target)},
            )

        temp = target.with_suffix(target.suffix + ".part")
        try:
            with temp.open("wb") as handle:
                handle.write(body)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, target)
        finally:
            if temp.exists():
                temp.unlink(missing_ok=True)

        return DownloadedImage(
            path=target,
            media_type=media_type,
            extension=extension,
            size_bytes=len(body),
            sha256=digest,
            width=width,
            height=height,
        )

    def _fetch(self, url: str, deadline: float) -> tuple[int, dict[str, str], bytes]:
        parts = urlsplit(url)
        host = parts.hostname
        if host is None:
            raise UnsafeResourceError("URL has no host", details={"url": url})
        port = parts.port or (443 if parts.scheme == "https" else 80)
        path = urlunsplit(("", "", parts.path or "/", parts.query, ""))
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise UnsafeResourceError("download deadline exceeded", details={"url": url})

        if parts.scheme == "https":
            connection: http.client.HTTPConnection = http.client.HTTPSConnection(
                host,
                port,
                timeout=min(remaining, self._timeout),
                context=ssl.create_default_context(),
            )
        else:
            connection = http.client.HTTPConnection(
                host, port, timeout=min(remaining, self._timeout)
            )

        try:
            connection.request(
                "GET",
                path,
                headers={
                    "User-Agent": _USER_AGENT,
                    # Advertise only what ``sniff_image`` accepts and what the
                    # Blender connector can load. Advertising AVIF invites
                    # servers to return a format we would then have to reject.
                    "Accept": "image/webp,image/png,image/jpeg,image/gif,image/bmp,*/*;q=0.5",
                    "Accept-Language": "en-US,en;q=0.9",
                },
            )
            response = connection.getresponse()
            headers = {key.lower(): value for key, value in response.getheaders()}
            declared = headers.get("content-length")
            if declared is not None:
                try:
                    if int(declared) > self._max_bytes:
                        raise UnsafeResourceError(
                            f"declared size {declared} exceeds limit {self._max_bytes}",
                            details={"url": url, "max_bytes": self._max_bytes},
                        )
                except ValueError:
                    pass

            chunks: list[bytes] = []
            total = 0
            while True:
                if time.monotonic() > deadline:
                    raise UnsafeResourceError("download deadline exceeded", details={"url": url})
                chunk = response.read(_CHUNK)
                if not chunk:
                    break
                total += len(chunk)
                if total > self._max_bytes:
                    raise UnsafeResourceError(
                        f"response exceeds the {self._max_bytes} byte limit",
                        details={"url": url, "max_bytes": self._max_bytes},
                    )
                chunks.append(chunk)
            return response.status, headers, b"".join(chunks)
        except (OSError, http.client.HTTPException) as exc:
            raise UnsafeResourceError(
                f"could not fetch {url}: {type(exc).__name__}: {exc}",
                details={"url": url},
            ) from exc
        finally:
            connection.close()


def _absolute_location(current: str, location: str) -> str:
    if location.startswith(("http://", "https://")):
        return location
    parts = urlsplit(current)
    if location.startswith("//"):
        return f"{parts.scheme}:{location}"
    base = f"{parts.scheme}://{parts.netloc}"
    if location.startswith("/"):
        return base + location
    directory = parts.path.rsplit("/", 1)[0] if "/" in parts.path else ""
    return f"{base}{directory}/{location}"


__all__ = [
    "DEFAULT_MAX_BYTES",
    "DownloadedImage",
    "ImageDownloader",
    "sniff_image",
    "validate_remote_url",
]
