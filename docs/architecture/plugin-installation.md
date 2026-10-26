# Plugin installation, integrity and what a hash is allowed to prove

A plugin is a `.wasm` file plus a manifest. This document is about the moment
between "there is a file on disk" and "the host runs it": which contradictions
are detected, which are refused, and which are explicitly *not* solved.

The short version, because it is the sentence most easily over-read:

> A SHA-256 pin proves that the bytes the host parses are the bytes somebody
> pinned. It does not prove **who** produced them, and it cannot, without a key
> distribution story this project does not have.

It also does not prove that anyone *decided* the plugin may run. That is a
different question with a different mechanism —
[plugin-lifecycle.md](plugin-lifecycle.md) covers approval, activation, revocation
and the three permission sets. The two documents have one sentence in common and
it is worth stating once here: **installing never approves, and a matching hash
never approves.**

## 1. Threat model

| # | Scenario | Detected? | By what |
| --- | --- | --- | --- |
| 1 | A developer installs a plugin they built | n/a — the normal path | `install()` records the hash |
| 2 | The file changes after install (editor, sync client, bad rebuild) | **yes** | re-hash at load; `PluginIntegrityError` |
| 3 | Somebody replaces the module with a different one | **yes**, if the pin is intact | manifest hash / install record |
| 4 | Manifest and module disagree | **yes** | hash comparison, `wasm_path` comparison, version comparison |
| 5 | A half-written file during upgrade | **yes** | the loader hashes the bytes it parses, in one read |
| 6 | Version / ABI incompatibility | **yes** | `interface_version` check (pre-existing) |

What none of these detect: an attacker who can rewrite **both** the module and
the manifest (or the record). Scenario 3 is only detectable while the pin lives
somewhere the attacker does not control, and by default it lives next to the
plugin. That is a deliberate, documented limit — see §8.

## 2. The install record

`install(manifest_path)` writes `deskweave.install.json` next to the manifest:

| Field | Why it is there |
| --- | --- |
| `record_version` | a format change gets a new number, never a best-effort parse |
| `plugin_id` | binds the pin to an identity, so two plugins cannot share one record |
| `version` | upgrade detection; a pin is per version |
| `interface_version` | records which ABI the pin was made against |
| `wasm_path` | the exact relative path that was pinned, so a rename is a contradiction |
| `wasm_sha256` | content identity |
| `size_bytes` | cheap sanity check; not a security control |
| `declared_capabilities` | detects a manifest that *grew* capabilities after install |

The last field is not an authorisation list. It is a **diff baseline**: a plugin
that adds `reference.metadata.delete` to its manifest after being pinned is
refused at load, because a plugin does not get to widen its own declaration
silently. Whether any capability may actually be called is still decided at call
time by the existing `PermissionManager` — the record never grants anything.

Since Phase 2.6 there is a second, stronger baseline: an *approval* records the
exact capability set a person agreed to for one build, and a manifest that grows a
capability is refused at the activation guard with `requires_reapproval=True` and
the `added` list. The pin says "the declaration did not change"; the approval says
"a person agreed to this set". Only the second one authorises.

## 3. Path rules

`Path(directory) / declared` is not a containment check. These shapes are
rejected lexically, before the filesystem is touched:

| Input | Why it is refused |
| --- | --- |
| `/etc/passwd`, `C:\Windows\x.wasm` | an absolute path replaces the base entirely |
| `\\server\share\x.wasm` | a network read, and a UNC path is "absolute" on Windows |
| `../x.wasm`, `sub/../../x.wasm` | traversal, including the sneaky middle form |
| `./x.wasm`, `a//b.wasm` | empty or `.` components make the path ambiguous |
| a path containing `\0` | truncation tricks in downstream APIs |
| a symlink (or a path through one) | a second name for bytes pinned under another name |

Backslashes are treated as separators on **every** platform, so a manifest means
the same thing on Windows and POSIX instead of meaning "a filename with a
backslash in it" on one of them.

After the lexical checks, `resolve()` must still land inside the directory. The
order matters: containment is checked last because it is the only rule that needs
the filesystem, and a symlink can defeat every lexical rule.

## 4. Three verification modes

| Mode | Manifest must declare a hash | Record required | What it catches |
| --- | --- | --- | --- |
| default (`WasmPluginHost()`) | no | no | nothing is enforced; the digest is recorded and logged |
| `require_integrity=True` | **yes** | no | modified file, manifest/module mismatch |
| `require_install_record=True` | yes, if present it must agree | **yes** | all of the above, plus post-install capability growth and version drift |

Default mode exists because every manifest written before Phase 2.5 has no hash,
and turning a missing field into a load failure would break every existing
caller. It is not a recommendation: a host that cares sets a flag.

"Missing hash" is never treated as "hash matched". In `require_integrity` mode a
manifest without `wasm_sha256` is refused — otherwise deleting one line would be
a bypass.

## 5. One read, one hash, one parse

The order in `load()` is deliberate:

1. read the module bytes **once** (or take the bytes `verify_install` hashed);
2. hash exactly those bytes;
3. parse exactly those bytes into a `wasmtime.Module`.

Hashing a file and then re-reading it to parse would leave a window in which the
file is swapped, and the check would be a statement about the previous contents.
The same reasoning makes the install record worthless if the loader reads past
the pinned file, so `verify_install` returns the bytes it hashed and the caller
uses them.

## 6. Upgrades and re-installs

| Situation | Behaviour |
| --- | --- |
| no record | `("installed", record)` |
| same plugin, same version, same hash, same capabilities | `("unchanged", record)`, nothing written |
| same plugin, different hash or version | `PluginInstallError` unless `replace=True` |
| different plugin in the same directory | `PluginInstallError`, always |
| crash during `install()` | old record or new record, never a truncated one (`os.replace`) |

A half-written **module** is not special-cased, because it does not need to be:
the pin describes complete bytes, so a partial file is simply a hash mismatch and
is refused before parsing.

## 7. Errors

| Code | Meaning |
| --- | --- |
| `plugin_integrity` | the bytes are not the bytes that were promised (path, hash, record, version) |
| `plugin_install` | pinning failed: version conflict, different plugin, or the manifest contradicts the file |

Both extend `PluginError`, so the existing taxonomy and `ErrorInfo` shape are
unchanged. Messages and `details` carry the **relative** path and the digests —
never the host's absolute directory, because error strings end up in logs and bug
reports.

## 8. The trust boundary (the part worth reading twice)

Four statements that are true, and one that is not:

- **True:** SHA-256 detects content changes. That is all it does.
- **True:** a local manifest is *not* an independent root of trust. If an attacker
  can write the module, they can usually write the manifest next to it.
- **True:** a user confirming "install this plugin" is a meaningful authorisation
  decision. It is not cryptographic origin verification.
- **True:** writing the record outside the plugin directory (a caller can pass
  `record_path`) raises the bar, because it removes one write target. It does not
  make the record unforgeable.
- **Not claimed:** "verified publisher". Nothing in this phase establishes that.

If publisher identity is needed later, it needs its own design: signature format
(which bytes are signed — the module only, or module + manifest?), a key trust
root, revocation, key rotation, and an upgrade policy that cannot be replayed.
Bolting a self-signed key onto the current flow would produce a green badge and
no additional guarantee, which is worse than the gap.

## 9. Evidence

Unit (`tests/unit/test_wasm_integrity.py`, 23 tests): path shapes, symlink,
streamed vs in-memory hashing, install idempotency, conflict refusal, atomic write
leaves no temp file, capability escalation, absolute-path leakage.

Load (`tests/integration/test_wasm_install_integrity.py`, 14 tests): all three
modes, modified module refused, missing hash refused when required, pinned plugin
loads, later swap refused, version drift refused, the shipped example matches its
own manifest.

Mutation (`scripts/wasm_guard_mutation_check.py`): `integrity-manifest-hash`,
`integrity-path-shape` and `install-conflict` remove each guard in turn and
require the suite to go red.
