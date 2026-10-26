"""Fixtures for the WASM plugin tests.

The positive and capability paths run the *real* Rust-built artifact committed
under ``examples/wasm-plugins/``. The deliberately hostile paths are hand-written
WAT modules: a spin loop, a memory hog and a malformed frame are easier to state
directly than to coax a real toolchain into producing, and they stay readable.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import wasmtime

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_DIR = REPO_ROOT / "examples" / "wasm-plugins" / "reference-metadata"
EXAMPLE_WASM = EXAMPLE_DIR / "reference_metadata.wasm"
EXAMPLE_MANIFEST = EXAMPLE_DIR / "reference_metadata.json"

ABI_VERSION = 1

BASE_MANIFEST: dict[str, Any] = {
    "plugin_id": "test-plugin",
    "name": "Test plugin",
    "version": "0.0.1",
    "interface_version": ABI_VERSION,
    "entry": "dw_invoke",
}

_PREAMBLE = """
  (memory (export "memory") 1)
  (func (export "dw_abi_version") (result i32) (i32.const {abi}))
  (func (export "dw_alloc") (param i32) (result i32) (i32.const 8192))
  (func (export "dw_free") (param i32 i32))
"""


def manifest(**overrides: Any) -> dict[str, Any]:
    """Return a minimal valid manifest with ``overrides`` applied."""
    merged = dict(BASE_MANIFEST)
    merged.update(overrides)
    return merged


def example_manifest(**overrides: Any) -> dict[str, Any]:
    """The manifest shipped with the example plugin, optionally patched.

    The integrity fields are dropped by default: they describe the bytes of
    ``reference_metadata.wasm``, and this helper is mostly used as a *template*
    for a renamed or rewritten copy. Carrying a hash that describes a different
    file would make every such test fail for an unrelated reason — and hiding
    that behind a weaker loader rule would be worse than fixing the fixture.
    """
    merged = json.loads(EXAMPLE_MANIFEST.read_text(encoding="utf-8"))
    merged.pop("wasm_path", None)
    merged.pop("wasm_sha256", None)
    merged.update(overrides)
    return merged


def _wat_bytes(data: bytes) -> str:
    """Escape ``data`` byte for byte for a WAT data segment.

    Escaping has to happen at the byte level, not through ``str``: a WAT data
    string is a byte string, and a round trip through ``str`` would re-encode
    anything above 0x7F. The adversarial fixtures need to place *invalid UTF-8*
    and other arbitrary bytes in guest memory, so this must be lossless.
    """
    out = []
    for byte in data:
        char = chr(byte)
        if char == '"':
            out.append('\\"')
        elif char == "\\":
            out.append("\\\\")
        elif 0x20 <= byte < 0x7F:
            out.append(char)
        else:
            out.append(f"\\{byte:02x}")
    return "".join(out)


def _wat_string(value: str) -> str:
    """Escape ``value``'s UTF-8 bytes for a WAT data segment."""
    return _wat_bytes(value.encode("utf-8"))


def _frame(body: str) -> str:
    """Build a length-prefixed frame and render it as a WAT data string."""
    raw = body.encode()
    framed = len(raw).to_bytes(4, "little") + raw
    return _wat_bytes(framed)


def write_plugin(
    directory: Path,
    wat: str,
    *,
    name: str = "plugin",
    manifest_dict: dict[str, Any] | None = None,
) -> Path:
    """Assemble ``name.wasm`` plus ``name.json`` in ``directory``; return the module path."""
    directory.mkdir(parents=True, exist_ok=True)
    module_path = directory / f"{name}.wasm"
    module_path.write_bytes(wasmtime.wat2wasm(wat))
    payload = manifest_dict if manifest_dict is not None else manifest(plugin_id=name)
    (directory / f"{name}.json").write_text(json.dumps(payload), encoding="utf-8")
    return module_path


# -- modules ----------------------------------------------------------------


def constant_response(body: str, *, abi: int = ABI_VERSION) -> str:
    """A module that always returns ``body`` as a well-framed response."""
    return f"""
(module
{_PREAMBLE.format(abi=abi)}
  (data (i32.const 4096) "{_frame(body)}")
  (func (export "dw_invoke") (param i32 i32) (result i32) (i32.const 4096))
)
"""


def lying_length_response(declared: int, *, abi: int = ABI_VERSION) -> str:
    """A module whose frame *claims* ``declared`` bytes but supplies none."""
    prefix = declared.to_bytes(4, "little").decode("latin-1")
    return f"""
(module
{_PREAMBLE.format(abi=abi)}
  (data (i32.const 4096) "{_wat_string(prefix)}")
  (func (export "dw_invoke") (param i32 i32) (result i32) (i32.const 4096))
)
"""


def spin_forever() -> str:
    """A module that never returns: fuel and timeout tests both need it."""
    return f"""
(module
{_PREAMBLE.format(abi=ABI_VERSION)}
  (func (export "dw_invoke") (param i32 i32) (result i32)
    (loop $forever (br $forever))
    (i32.const 0))
)
"""


def memory_hog() -> str:
    """Grow memory until the allocator refuses, then answer normally."""
    return f"""
(module
{_PREAMBLE.format(abi=ABI_VERSION)}
  (data (i32.const 4096) "{_frame('{"ok":true}')}")
  (func (export "dw_invoke") (param i32 i32) (result i32)
    (local $grew i32)
    (local.set $grew (i32.const 1))
    (block $done
      (loop $grow
        (br_if $done (i32.eqz (local.get $grew)))
        (local.set $grew
          (i32.ge_s (memory.grow (i32.const 4)) (i32.const 0)))
        (br $grow)))
    (i32.const 4096))
)
"""


def trap_on_invoke() -> str:
    """A module that traps immediately (``unreachable``)."""
    return f"""
(module
{_PREAMBLE.format(abi=ABI_VERSION)}
  (func (export "dw_invoke") (param i32 i32) (result i32)
    (unreachable))
)
"""


def exit_on_invoke(code: int = 3) -> str:
    """A module that tries to leave through ``proc_exit`` instead of returning."""
    return f"""
(module
  (import "wasi_snapshot_preview1" "proc_exit" (func $exit (param i32)))
  (memory (export "memory") 1)
  (func (export "dw_abi_version") (result i32) (i32.const {ABI_VERSION}))
  (func (export "dw_alloc") (param i32) (result i32) (i32.const 8192))
  (func (export "dw_free") (param i32 i32))
  (func (export "dw_invoke") (param i32 i32) (result i32)
    (call $exit (i32.const {code}))
    (i32.const 0))
)
"""


def missing_entry_point() -> str:
    """A module that exports the ABI but not the entry function."""
    return f"""
(module
  (memory (export "memory") 1)
  (func (export "dw_abi_version") (result i32) (i32.const {ABI_VERSION}))
  (func (export "dw_alloc") (param i32) (result i32) (i32.const 8192))
  (func (export "dw_free") (param i32 i32))
)
"""


def out_of_bounds_writer() -> str:
    """Grow as much as allowed, then write past the end of memory.

    With a small memory cap the grow is refused, so the store below lands outside
    linear memory and traps. That is the "allocation that does not check its
    result" case, and the host must contain it rather than crash.
    """
    return f"""
(module
{_PREAMBLE.format(abi=ABI_VERSION)}
  (func (export "dw_invoke") (param i32 i32) (result i32)
    (drop (memory.grow (i32.const 4)))
    (i32.store (i32.const 67108864) (i32.const 1))
    (i32.const 4096))
)
"""


def declares_too_much_memory(pages: int = 100) -> str:
    """A module whose *minimum* memory already exceeds a small host cap."""
    return f"""
(module
  (memory (export "memory") {pages})
  (func (export "dw_abi_version") (result i32) (i32.const {ABI_VERSION}))
  (func (export "dw_alloc") (param i32) (result i32) (i32.const 8192))
  (func (export "dw_free") (param i32 i32))
  (data (i32.const 4096) "{_frame("{}")}")
  (func (export "dw_invoke") (param i32 i32) (result i32) (i32.const 4096))
)
"""


def declares_too_large_a_table(elements: int = 4_000_000) -> str:
    """A module whose table alone is bigger than the host's memory budget.

    A table entry is a reference, not a page. It is never charged against
    ``memory_size``, nothing calls ``memory.grow`` to reach it, and the bytes it
    occupies are real — 4 000 000 funcref is 32 MB, and the ceiling used to be
    whatever ``MAX_FUEL`` allowed the module to fill. Exports are complete so the
    refusal can only come from the table.
    """
    return f"""
(module
  (table {elements} funcref)
  (memory (export "memory") 1)
  (func (export "dw_abi_version") (result i32) (i32.const {ABI_VERSION}))
  (func (export "dw_alloc") (param i32) (result i32) (i32.const 8192))
  (func (export "dw_free") (param i32 i32))
  (data (i32.const 4096) "{_frame("{}")}")
  (func (export "dw_invoke") (param i32 i32) (result i32) (i32.const 4096))
)
"""


def declares_two_memories() -> str:
    """Two linear memories under a cap that is stated in bytes, not per memory.

    ``memory_size`` bounds *each* memory, so an unbounded count multiplies the
    budget by however many a module declares. Exports are complete so the
    refusal can only come from the count.
    """
    return f"""
(module
  (memory (export "memory") 1)
  (memory 1)
  (func (export "dw_abi_version") (result i32) (i32.const {ABI_VERSION}))
  (func (export "dw_alloc") (param i32) (result i32) (i32.const 8192))
  (func (export "dw_free") (param i32 i32))
  (data (i32.const 4096) "{_frame("{}")}")
  (func (export "dw_invoke") (param i32 i32) (result i32) (i32.const 4096))
)
"""


def calls_host_then_claims_success(request_json: str) -> str:
    """A module that ignores a host denial and returns a success payload.

    This is the "lying plugin" case: the host must fail the invocation anyway,
    because the denial — not the plugin's summary — is the ground truth.
    """
    request = _wat_string(request_json)
    req_len = len(request_json.encode())
    return f"""
(module
  (import "env" "dw_host_call" (func $host (param i32 i32 i32 i32) (result i32)))
  (memory (export "memory") 1)
  (data (i32.const 0) "{request}")
  (data (i32.const 4096) "{_frame('{"checked":0,"accepted":[],"rejected":[]}')}")
  (func (export "dw_abi_version") (result i32) (i32.const {ABI_VERSION}))
  (func (export "dw_alloc") (param i32) (result i32) (i32.const 8192))
  (func (export "dw_free") (param i32 i32))
  (func (export "dw_invoke") (param i32 i32) (result i32)
    (drop (call $host (i32.const 0) (i32.const {req_len}) (i32.const 6000) (i32.const 4096)))
    (i32.const 4096))
)
"""


# -- adversarial modules ------------------------------------------------------
#
# Everything below is a *hostile* module: it does something the ABI does not
# allow and expects the host to contain it. They exist so that "the host
# refuses" is a measurement rather than a claim.

_HOST_MEMORY_PAGES = 4  # 256 KiB: enough for probes that need room to misbehave


def _host_module(
    invoke_body: str,
    *,
    data: str = "",
    mem_pages: int = _HOST_MEMORY_PAGES,
) -> str:
    """A module that exports the ABI and runs ``invoke_body`` inside ``dw_invoke``.

    ``data`` is spliced at module level (data segments cannot live inside a
    function) and ``invoke_body`` inside ``dw_invoke``. The response frame sits at
    a fixed 128 KiB so probes have the low addresses to themselves.
    """
    return f"""
(module
  (import "env" "dw_host_call" (func $host (param i32 i32 i32 i32) (result i32)))
  (memory (export "memory") {mem_pages})
  (func (export "dw_abi_version") (result i32) (i32.const {ABI_VERSION}))
  (func (export "dw_alloc") (param i32) (result i32) (i32.const 65536))
  (func (export "dw_free") (param i32 i32))
  {data}
  (data (i32.const 131072) "{_frame('{"ok":true}')}")
  (func (export "dw_invoke") (param i32 i32) (result i32)
{invoke_body}
    (i32.const 131072))
)
"""


def raw_host_request(
    raw: bytes,
    *,
    out_ptr: int = 200000,
    out_cap: int = 40000,
    mem_pages: int = _HOST_MEMORY_PAGES,
) -> str:
    """Send ``raw`` bytes as a host-call request, whatever they happen to be.

    One fixture covers malformed JSON, invalid UTF-8, a non-object body, a
    truncated document and a length that lies about itself — the host must treat
    all of them the same way: refuse, record, and carry on.
    """
    segment = f'(data (i32.const 0) "{_wat_bytes(raw)}")'
    body = (
        f"    (drop (call $host (i32.const 0) (i32.const {len(raw)}) "
        f"(i32.const {out_ptr}) (i32.const {out_cap})))"
    )
    return _host_module(body, data=segment, mem_pages=mem_pages)


def host_request(request_json: str, **kwargs: Any) -> str:
    """Send ``request_json`` as the host-call request."""
    return raw_host_request(request_json.encode(), **kwargs)


def host_request_bad_out_ptr(request_json: str, out_ptr: int, out_cap: int = 4096) -> str:
    """A well-formed request with an unwritable output buffer."""
    return host_request(request_json, out_ptr=out_ptr, out_cap=out_cap)


def host_request_bad_req_ptr(request_json: str) -> str:
    """Point the request at an address past the end of the module's memory."""
    raw = request_json.encode()
    segment = f'(data (i32.const 0) "{_wat_bytes(raw)}")'
    body = (
        f"    (drop (call $host (i32.const 900000) (i32.const {len(raw)}) "
        f"(i32.const 200000) (i32.const 40000)))"
    )
    return _host_module(body, data=segment)


def nested_host_request(depth: int) -> str:
    """A request whose payload nests ``depth`` arrays, to make the decoder recurse."""
    body = "[" * depth + "]" * depth
    request = f'{{"capability":"reference.metadata.validate","payload":{{"x":{body}}}}}'
    return host_request(request)


def alloc_beyond_memory(ptr: int) -> str:
    """``dw_alloc`` claims a buffer that is not inside linear memory."""
    return f"""
(module
  (memory (export "memory") 1)
  (func (export "dw_abi_version") (result i32) (i32.const {ABI_VERSION}))
  (func (export "dw_alloc") (param i32) (result i32) (i32.const {ptr}))
  (func (export "dw_free") (param i32 i32))
  (func (export "dw_invoke") (param i32 i32) (result i32) (i32.const 0))
)
"""


def trapping_start() -> str:
    """The module traps while being instantiated, before ``dw_invoke`` exists."""
    return f"""
(module
{_PREAMBLE.format(abi=ABI_VERSION)}
  (func $boom (unreachable))
  (start $boom)
  (func (export "dw_invoke") (param i32 i32) (result i32) (i32.const 0))
)
"""


def trapping_abi_version() -> str:
    """``dw_abi_version`` itself traps, so verification runs guest code that fails."""
    return """
(module
  (memory (export "memory") 1)
  (func (export "dw_abi_version") (result i32) (unreachable))
  (func (export "dw_alloc") (param i32) (result i32) (i32.const 4096))
  (func (export "dw_free") (param i32 i32))
  (func (export "dw_invoke") (param i32 i32) (result i32) (i32.const 0))
)
"""


def wrong_signature(export: str) -> str:
    """Export the ABI with one signature that is not the one the host calls."""
    common = {
        "dw_abi_version": (
            f'(func (export "dw_abi_version") (result i32) (i32.const {ABI_VERSION}))'
        ),
        "dw_alloc": '(func (export "dw_alloc") (param i32) (result i32) (i32.const 4096))',
        "dw_invoke": '(func (export "dw_invoke") (param i32 i32) (result i32) (i32.const 0))',
    }
    broken = {
        "dw_abi_version": '(func (export "dw_abi_version") (result i64) (i64.const 1))',
        "dw_alloc": '(func (export "dw_alloc") (result i32) (i32.const 4096))',
        "dw_invoke": '(func (export "dw_invoke") (param i32) (result i32) (i32.const 0))',
        "dw_invoke_void": '(func (export "dw_invoke") (param i32 i32))',
    }
    if export not in broken:
        raise ValueError(f"no broken variant for {export!r}")
    common["dw_invoke" if export == "dw_invoke_void" else export] = broken[export]
    return f"""
(module
  (memory (export "memory") 1)
  (func (export "dw_free") (param i32 i32))
  {common["dw_abi_version"]}
  {common["dw_alloc"]}
  {common["dw_invoke"]}
)
"""


def repeated_host_calls(count: int, request_json: str) -> str:
    """Call the host ``count`` times in one invocation, ignoring every status."""
    raw = request_json.encode()
    segment = f'(data (i32.const 0) "{_wat_bytes(raw)}")'
    call = f"(call $host (i32.const 0) (i32.const {len(raw)})"
    body = f"""    (local $i i32)
    (block $done
      (loop $again
        (br_if $done (i32.ge_s (local.get $i) (i32.const {count})))
        (drop {call} (i32.const 200000) (i32.const 40000)))
        (local.set $i (i32.add (local.get $i) (i32.const 1)))
        (br $again)))"""
    return _host_module(body, data=segment)


def repeated_refused_calls(count: int) -> str:
    """Call the host ``count`` times with a request pointer outside memory.

    Every attempt is refused *before* dispatch, which is exactly what makes this
    a test of whether the budget counts attempts or dispatches: if it counted
    dispatches, these calls would all be free.
    """
    body = f"""    (local $i i32)
    (block $done
      (loop $again
        (br_if $done (i32.ge_s (local.get $i) (i32.const {count})))
        (drop (call $host (i32.const 900000) (i32.const 8) (i32.const 200000) (i32.const 40000)))
        (local.set $i (i32.add (local.get $i) (i32.const 1)))
        (br $again)))"""
    return _host_module(body)


__all__ = [
    "BASE_MANIFEST",
    "EXAMPLE_DIR",
    "EXAMPLE_MANIFEST",
    "EXAMPLE_WASM",
    "REPO_ROOT",
    "alloc_beyond_memory",
    "calls_host_then_claims_success",
    "constant_response",
    "declares_too_large_a_table",
    "declares_too_much_memory",
    "declares_two_memories",
    "example_manifest",
    "exit_on_invoke",
    "host_request",
    "host_request_bad_out_ptr",
    "host_request_bad_req_ptr",
    "lying_length_response",
    "manifest",
    "memory_hog",
    "missing_entry_point",
    "nested_host_request",
    "out_of_bounds_writer",
    "raw_host_request",
    "repeated_host_calls",
    "repeated_refused_calls",
    "spin_forever",
    "trap_on_invoke",
    "trapping_abi_version",
    "trapping_start",
    "write_plugin",
    "wrong_signature",
]
