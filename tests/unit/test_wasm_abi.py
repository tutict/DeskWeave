"""Unit tests for the plugin ABI frame codec."""

from __future__ import annotations

import pytest

from deskweave_plugin_wasm.abi import (
    MAX_FRAME_BYTES,
    decode_frame,
    encode_frame,
    frame_length,
)
from deskweave_plugin_wasm.errors import PluginInvalidOutputError


def test_round_trip() -> None:
    body = b'{"hello":"world"}'
    framed = encode_frame(body)
    assert frame_length(framed[:4]) == len(body)
    assert decode_frame(framed) == body


def test_empty_body_round_trips() -> None:
    assert decode_frame(encode_frame(b"")) == b""


def test_prefix_is_little_endian_u32() -> None:
    framed = encode_frame(b"abc")
    assert framed[:4] == b"\x03\x00\x00\x00"


def test_truncated_header_is_rejected() -> None:
    with pytest.raises(PluginInvalidOutputError):
        frame_length(b"\x01")


def test_decode_rejects_body_shorter_than_declared() -> None:
    framed = encode_frame(b"abcd")
    with pytest.raises(PluginInvalidOutputError):
        decode_frame(framed[:6])


def test_decode_rejects_frame_over_limit() -> None:
    framed = encode_frame(b"0123456789")
    with pytest.raises(PluginInvalidOutputError) as excinfo:
        decode_frame(framed, max_bytes=4)
    assert excinfo.value.details["bytes"] == 10


def test_encode_rejects_oversized_body() -> None:
    with pytest.raises(PluginInvalidOutputError):
        encode_frame(b"x" * (MAX_FRAME_BYTES + 1))


def test_decoding_ignores_bytes_after_the_frame() -> None:
    framed = encode_frame(b"body") + b"trailing-garbage"
    assert decode_frame(framed) == b"body"
