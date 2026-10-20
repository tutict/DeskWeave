"""Unit tests for the image downloader's safety policy.

These tests exercise the guard rails that stand between "a search result URL" and
"a file on disk": scheme/port/address filtering, redirect re-validation, the byte
cap, content sniffing and atomic writes. A local HTTP server is used so the tests
never depend on the public internet, and the URL guard is disabled *explicitly*
(never by default) to allow loopback connections.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import pytest

from deskweave_contracts import UnsafeResourceError
from deskweave_runtime.artifacts.downloader import (
    ImageDownloader,
    sniff_image,
    validate_remote_url,
)


def make_png(width: int = 4, height: int = 3) -> bytes:
    """Build a real, minimal RGB PNG so sniffing is tested against valid bytes."""
    import struct
    import zlib

    raw = b"".join(b"\x00" + b"\x10\x20\x30" * width for _ in range(height))

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + tag
            + payload
            + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
        )

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


# -- URL policy -----------------------------------------------------------
@pytest.mark.parametrize(
    "url",
    [
        "file:///C:/Windows/win.ini",
        "ftp://example.com/a.png",
        "javascript:alert(1)",
        "data:image/png;base64,AAAA",
    ],
)
def test_non_http_schemes_are_rejected(url: str) -> None:
    with pytest.raises(UnsafeResourceError) as excinfo:
        validate_remote_url(url)
    assert "scheme" in excinfo.value.message


@pytest.mark.parametrize(
    "url",
    ["https://user:pass@example.com/a.png", "https://user@example.com/a.png"],
)
def test_embedded_credentials_are_rejected(url: str) -> None:
    with pytest.raises(UnsafeResourceError) as excinfo:
        validate_remote_url(url)
    assert "credentials" in excinfo.value.message


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/a.png",
        "http://localhost/a.png",
        "http://[::1]/a.png",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.5/a.png",
        "http://192.168.1.1/a.png",
        "http://0.0.0.0/a.png",
    ],
)
def test_loopback_and_private_addresses_are_rejected(url: str) -> None:
    with pytest.raises(UnsafeResourceError):
        validate_remote_url(url)


def test_non_standard_port_is_rejected() -> None:
    with pytest.raises(UnsafeResourceError) as excinfo:
        validate_remote_url("http://93.184.216.34:8080/a.png")
    assert "port" in excinfo.value.message


def test_public_numeric_address_is_accepted() -> None:
    # Numeric addresses avoid DNS, so this stays hermetic.
    validate_remote_url("https://93.184.216.34/reference.png")


# -- content sniffing -----------------------------------------------------
def test_sniff_png_reports_dimensions() -> None:
    media_type, extension, width, height = sniff_image(make_png(12, 7))
    assert (media_type, extension, width, height) == ("image/png", ".png", 12, 7)


def test_sniff_jpeg() -> None:
    # Minimal SOI + SOF0 frame with height=5, width=9.
    payload = (
        b"\xff\xd8"
        b"\xff\xc0\x00\x11\x08\x00\x05\x00\x09\x03\x01\x22\x00\x02\x11\x01\x03\x11\x01"
        b"\xff\xd9"
    )
    media_type, extension, width, height = sniff_image(payload)
    assert (media_type, extension, width, height) == ("image/jpeg", ".jpg", 9, 5)


def test_sniff_gif() -> None:
    payload = b"GIF89a" + (6).to_bytes(2, "little") + (4).to_bytes(2, "little") + b"\x00" * 8
    assert sniff_image(payload)[:4] == ("image/gif", ".gif", 6, 4)


def test_svg_is_rejected() -> None:
    svg = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
    with pytest.raises(UnsafeResourceError):
        sniff_image(svg)


def test_html_is_rejected() -> None:
    with pytest.raises(UnsafeResourceError):
        sniff_image(b"<!doctype html><html><body>not an image</body></html>")


def test_empty_payload_is_rejected() -> None:
    with pytest.raises(UnsafeResourceError):
        sniff_image(b"")


# -- downloading ----------------------------------------------------------
class _Server:
    """Serves a tiny, deterministic set of routes for download tests."""

    def __init__(self) -> None:
        self.hits: list[str] = []
        self._png = make_png(8, 6)
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self) -> None:
                outer.hits.append(self.path)
                if self.path == "/ok.png":
                    self._send(200, "image/png", outer._png)
                elif self.path == "/huge.png":
                    self._send(200, "image/png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 4096)
                elif self.path == "/page.html":
                    self._send(200, "image/png", b"<html>pretends to be png</html>")
                elif self.path == "/redirect":
                    self.send_response(302)
                    self.send_header("Location", "/ok.png")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                elif self.path == "/to-localhost":
                    self.send_response(302)
                    self.send_header("Location", "http://127.0.0.1:9/secret.png")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                elif self.path == "/loop":
                    self.send_response(302)
                    self.send_header("Location", "/loop")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                elif self.path == "/missing.png":
                    self._send(404, "text/plain", b"nope")
                else:
                    self._send(404, "text/plain", b"unknown")

            def _send(self, status: int, content_type: str, body: bytes) -> None:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args: object) -> None:  # silence stderr noise
                return

        class Server(ThreadingHTTPServer):
            def handle_error(self, request: object, client_address: object) -> None:
                # Clients that abort mid-body (size-cap tests) are expected.
                return

        self.httpd = Server(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def __enter__(self) -> _Server:
        self.thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)


@pytest.fixture
def server() -> Iterator[_Server]:
    with _Server() as instance:
        yield instance


def _downloader(tmp_path, **kwargs) -> ImageDownloader:
    # The guard is disabled deliberately: loopback is exactly what the policy
    # blocks, and these tests target the *other* layers.
    return ImageDownloader(tmp_path / "images", url_guard=lambda url: None, **kwargs)


async def test_download_stores_file_with_metadata(server: _Server, tmp_path) -> None:
    downloader = _downloader(tmp_path)
    result = await downloader.download(server.url("/ok.png"), artifact_id="art_test")

    assert result.path == (tmp_path / "images" / "art_test.png")
    assert result.path.read_bytes() == make_png(8, 6)
    assert result.media_type == "image/png"
    assert (result.width, result.height) == (8, 6)
    assert result.size_bytes == len(make_png(8, 6))
    assert len(result.sha256) == 64
    assert not (tmp_path / "images" / "art_test.png.part").exists()


async def test_download_enforces_size_cap(server: _Server, tmp_path) -> None:
    downloader = _downloader(tmp_path, max_bytes=512)
    with pytest.raises(UnsafeResourceError) as excinfo:
        await downloader.download(server.url("/huge.png"), artifact_id="art_big")
    assert "exceeds" in excinfo.value.message
    assert not (tmp_path / "images" / "art_big.png").exists()


async def test_download_rejects_non_image_body(server: _Server, tmp_path) -> None:
    downloader = _downloader(tmp_path)
    with pytest.raises(UnsafeResourceError):
        await downloader.download(server.url("/page.html"), artifact_id="art_html")
    assert not (tmp_path / "images" / "art_html.png").exists()


async def test_download_rejects_http_error(server: _Server, tmp_path) -> None:
    downloader = _downloader(tmp_path)
    with pytest.raises(UnsafeResourceError) as excinfo:
        await downloader.download(server.url("/missing.png"), artifact_id="art_404")
    assert "404" in excinfo.value.message


async def test_redirect_is_followed(server: _Server, tmp_path) -> None:
    downloader = _downloader(tmp_path)
    result = await downloader.download(server.url("/redirect"), artifact_id="art_redir")
    assert result.path.exists()
    assert server.hits == ["/redirect", "/ok.png"]


async def test_redirect_target_is_revalidated(server: _Server, tmp_path) -> None:
    """A public URL that redirects to loopback must still be refused."""
    seen: list[str] = []

    def guard(url: str) -> None:
        seen.append(url)
        # Compare the *port*, not a substring: the local test server's own port is
        # random and may well start with "9" (e.g. 9834), which would make a
        # substring test reject the very first hop and prove nothing.
        if urlsplit(url).port == 9:
            raise UnsafeResourceError("non-public address", details={"url": url})

    downloader = ImageDownloader(tmp_path / "images", url_guard=guard)
    with pytest.raises(UnsafeResourceError):
        await downloader.download(server.url("/to-localhost"), artifact_id="art_ssrf")
    assert len(seen) == 2, "the guard must run again on every redirect hop"
    assert not (tmp_path / "images" / "art_ssrf.png").exists()


async def test_redirect_loop_is_bounded(server: _Server, tmp_path) -> None:
    downloader = _downloader(tmp_path, max_redirects=2)
    with pytest.raises(UnsafeResourceError) as excinfo:
        await downloader.download(server.url("/loop"), artifact_id="art_loop")
    assert "redirect" in excinfo.value.message


async def test_declared_oversized_content_length_is_rejected(server: _Server, tmp_path) -> None:
    downloader = _downloader(tmp_path, max_bytes=1024)
    with pytest.raises(UnsafeResourceError):
        await downloader.download(server.url("/huge.png"), artifact_id="art_decl")
