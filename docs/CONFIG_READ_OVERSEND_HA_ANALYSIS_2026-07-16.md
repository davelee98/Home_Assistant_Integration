# Config-read over-send: HA-side + write-path analysis (2026-07-16)

Working investigation note (not user docs). Follow-up to
`ENCRYPTED_SILABS_FWVERSION_CRASH_2026-07-16.md`, which traced the crash to a
config-read that over-sends (670 bytes for a declared 613) and leaves stale
frames that later poison `read_firmware_version()`. That note flagged two open
questions the static read could not settle:

- **(b)** could `handle_config_read` be entered more than once (re-trigger /
  concurrency)?
- **(a)** is the stored blob simply larger than its own wrapper length?

This note answers both from the code: what on the **Home Assistant side** can
issue `config_read` more than once, and — reading the Silabs firmware storage +
write path — what can make a *single* clean read legitimately over-send with no
concurrency at all. The second turns out to be the stronger lead.

## TL;DR

1. **HA drives multiple back-to-back interrogations during onboarding.** An
   encrypted device is interrogated up to **three** times in a few seconds
   (plaintext probe → encrypted probe → `async_setup_entry`), across independent
   connections that share **no serialization** — the `ble_lock` doesn't exist
   until setup completes. This can *overlap* two config-reads on one link.
2. **But the current firmware ties advertised length and streamed length to a
   single variable**, so a single clean read cannot deliver more than it
   advertises. That weakens the concurrency hypothesis for the *over-send*
   specifically.
3. **The strongest non-concurrency cause is a write-path length bug.**
   `handle_config_chunk` accumulates `received_size` and completes on a *chunk
   count*, never clamping to the declared `total_size`, then stores
   `data_len = received_size`. An over-long/padded write persists an inflated
   `data_len` that read-back streams verbatim — matching "declared 613,
   delivered 670" with **no concurrency, no re-entrancy, and CRC won't catch
   it.**

`data_len` is a **plaintext** quantity end to end — encryption is stripped
before it is measured (write) and added after chunking (read) — so the over-send
is not an encryption-overhead accounting error.

---

## Part 1 — What the HA integration does during initial configuration

### Where `config_read` comes from

`config_read` is issued by `OpenDisplayDevice.__aenter__` **only when the device
is opened without a `config=` argument** (`py-opendisplay device.py:576-579`,
auto-interrogate). So the trigger is purely a function of how HA opens the link:

| Open site | Passes `config=`? | Auto-interrogates? |
|---|---|---|
| `config_flow.py:159` (`_async_test_connection`) | no | **yes** |
| `__init__.py:238` (`async_setup_entry`) | no | **yes** |
| `update.py:261` (OTA DFU trigger) | no | **yes** |
| `delivery.py:323` (`_drain_once`) | yes (`runtime.device_config`) | no |
| `services.py:437` (draw/led/buzzer) | yes | no |

Every interrogation happens in a **setup/onboarding path**; the steady-state
runtime paths suppress it by passing the cached config.

### The onboarding sequence for an encrypted device

1. **Probe #1 — plaintext.** `bluetooth_confirm`/`user` call
   `_async_test_connection(address)` with **no key**. `__aenter__` skips
   `authenticate()` and auto-interrogates, so a **plaintext `config_read`
   command is written on the wire**. The encrypted device replies with the
   3-byte `[cmd_hi, cmd_lo, 0xFE]` "auth required" frame; the client's `_read`
   raises `AuthenticationRequiredError` (`device.py:723-726`) → HA prompts for
   the key. *(The command is issued, but the device returns a 3-byte reject, not
   a config stream — so no 613/670 payload here.)*
2. **Probe #2 — encrypted.** `async_step_encryption_key` calls
   `_async_test_connection(address, key)`: authenticate, auto-interrogate →
   full **encrypted config stream**, then `read_firmware_version()`. Entry
   created.
3. **`async_setup_entry`.** Entry creation immediately connects **again**,
   authenticates, auto-interrogates → **another encrypted config stream**, then
   `read_firmware_version()` at `__init__.py:243` — the exact line the crash log
   stops at.

So counting reads that actually *stream* config, encrypted onboarding does
**two** (probe #2 + setup) plus one rejected plaintext command. A plaintext
(non-encrypted) device gets a single successful stream on its one keyless probe
— consistent with "only reproduces with encryption."

### Why these can overlap: no serialization before setup

The lock that serializes every runtime BLE op —
`OpenDisplayRuntimeData.ble_lock` (`__init__.py:78`) — lives in
`entry.runtime_data`, which **doesn't exist until `async_setup_entry`
finishes** (`__init__.py:305`). Consequences during onboarding:

- **Probe → setup handoff is unguarded.** `async with OpenDisplayDevice(...)`
  awaits `disconnect()` on exit, but an ESP32 `bluetooth_proxy` disconnect can
  lag/coalesce, so setup's config-read can begin while probe #2's frames are
  still draining.
- **Concurrent config flows.** `async_step_user` uses
  `async_set_unique_id(address, raise_on_progress=False)`
  (`config_flow.py:221`) — unlike the discovery step, it does **not** abort when
  a bluetooth-discovery flow for the same address is in progress. A manual add
  concurrent with passive discovery yields two flows, each opening its own
  connection to the same MAC.

### Additional interrogate triggers beyond onboarding

Anything that re-runs `async_setup_entry` re-interrogates:

- **Reboot-edge reload** — coordinator fires on the advertised reboot flag
  False→True (`coordinator.py:151-171`); for a **non-sleepy** device
  `_schedule_reboot_reload` calls `async_reload` → setup → interrogate
  (`__init__.py:340-352`). **Dangerous during onboarding:** if a config-read
  crashes+reboots the device, the next advertisement shows reboot=True → reload
  → interrogate → crash → loop.
- **Options save** — `OptionsFlowWithReload` reloads on save.
- **Reauth** — `async_step_reauth_confirm` probes (interrogate) *then*
  `async_update_reload_and_abort` reloads (interrogate) = two.
- **`ConfigEntryNotReady` retry backoff** — each retry reconnects + interrogates.

Note: the resync path (`delivery._drain_resync`) opens **with** `config=`
(`delivery.py:326`), so it does **not** auto-interrogate — it only re-reads
firmware and then re-reads the *same* cached config. (Minor: the method is
documented as "re-read firmware/config" but never re-fetches config; harmless
here, but the comment/`_write_cache` imply a fresh config that isn't fetched.)

---

## Part 2 — Reading the firmware: a single read cannot out-run its own prefix

`handle_config_read` (`Firmware_Silabs/opendisplay_pipe.c:724-789`):

```c
uint32_t config_len = MAX_CONFIG_SIZE;
loadConfig(config_data, &config_len);      // config_len := hdr.data_len
...
s_cfg_read_buf[...] = (uint8_t)(config_len & 0xFF);        // chunk-0 length prefix
s_cfg_read_buf[...] = (uint8_t)((config_len >> 8) & 0xFF);
...
uint32_t remaining = config_len;           // total streamed
```

The **same** `config_len` drives the chunk-0 length prefix *and* the total
streamed, and each chunk is `chunk_size = min(remaining, max_data)` with a short
final chunk. `MAX_RESPONSE_DATA_SIZE = 100` gives the observed 94 (chunk 0) /
96 (later) payloads.

**Implication:** in this firmware, advertised length == streamed length. A
single clean `config_read` can never deliver more bytes than its own prefix
says. So "declared 613 but delivered 670" from *one* clean read is only possible
if either (i) `hdr.data_len` genuinely *is* ~670 and the "613" is the inner TLV
wrapper embedded in the payload, or (ii) extra chunks arrive from **outside**
this stream (a second stream, or transport residue/replay).

---

## Part 3 — Tracing `hdr.data_len`

### Consumers

- **`saveConfig`** (`opendisplay_config_storage.c:59-88`) — sets
  `s_cfg_rec.data_len = len` and writes `total = header_sz + len` to NVM3. The
  object is **exactly** header + `data_len`; there is **no storage padding**
  (this rules out the NVM3-slack variant of hypothesis (a)).
- **`loadConfig`** (`:90-139`) — validates (`> MAX_CONFIG_SIZE`,
  `> obj_len - header_sz`, `> *len`), reads exactly `data_len` bytes, CRC-checks
  over `data_len`, returns `*len = hdr.data_len`.
- **`calculateConfigCRC(data, data_len)`** — CRC over exactly `data_len` bytes
  on **both** save and load. Because both ends use the same length, an inflated
  `data_len` stays self-consistent and **still passes CRC**. CRC does not detect
  a wrong length.
- **`handle_config_read`** — `config_len = data_len` → prefix + stream (above).
- **`opendisplay_ble_reload_config_from_nvm`** (`opendisplay_ble.c:1496`) →
  `loadConfig` at `opendisplay_config_parser.c:523` — re-parses NVM3 into the
  live model after every write; consumes `data_len` via `loadConfig`.
- On the write side, **`s_cfg_chunk.received_size`** *becomes* `data_len` via
  `saveConfig` (`opendisplay_pipe.c:839, 859, 900`).

### Before or after stripping encryption

**After — `data_len` is a plaintext quantity end to end.**

- **Write:** `on_pipe_write` (`:1225-1236`) decrypts first: for a live session,
  frames `>= 31` bytes go through
  `decrypt_encrypted_payload(..., s_plain_buf, &plain_len)` then
  `dispatch(connection, cmd, s_plain_buf, plain_len)`. `handle_config_write` /
  `handle_config_chunk` accumulate `plain_len` into `received_size`. The
  nonce(16)+tag(12)+`[len:1]` envelope is stripped **before** the length is
  measured.
- **Read:** `handle_config_read` chunks the plaintext (`config_len = data_len`);
  `pipe_send`/`encrypt_response_payload` add the envelope **after** `chunk_size`
  is chosen. The client decrypts back to plaintext and counts plaintext.

So the over-send is **not** an encryption-overhead accounting error.

> ⚠️ **Latent decrypt-boundary hole.** `on_pipe_write` only decrypts frames
> `>= 31` bytes; shorter frames fall through to
> `dispatch(connection, cmd, &frame[2], frame_len - 2)` (`:1238`) **raw**. A real
> encrypted frame is always ≥31 B, but a truncated/malformed final config-chunk
> arriving <31 B during an encrypted session would be memcpy'd **undecrypted**
> into the config buffer and its raw length counted. Narrow, but it is a path
> where encrypted-but-unstripped bytes could pollute both the stored config and
> its `data_len`.

---

## Part 4 — The leading non-concurrency root cause (write path)

`handle_config_chunk` (`opendisplay_pipe.c:867-910`):

```c
memcpy(opendisplay_config_buf() + s_cfg_chunk.received_size, data, len);
s_cfg_chunk.received_size += len;                 // NOT clamped to total_size
s_cfg_chunk.received_chunks++;

if (s_cfg_chunk.received_chunks >= s_cfg_chunk.expected_chunks) {  // COUNT, not bytes
  saveConfig(opendisplay_config_buf(), s_cfg_chunk.received_size); // stores received_size
```

- The **first** frame clamps `chunk_data_size` to `total_size`
  (`handle_config_write:818-820`).
- But **subsequent** chunks (`handle_config_chunk`) never clamp `received_size`
  to the declared `total_size`, and completion is a **chunk count**
  (`received_chunks >= expected_chunks`), not a byte total.

So if the writing client sends more data bytes than its own `total_size` prefix
declared (an over-long or padded final chunk), the firmware persists
`data_len = received_size > total_size`. Read-back streams that inflated length
verbatim, the chunk-0 prefix reflects it, and the inner config wrapper still
declares the true 613 → **exactly the "declared 613, delivered 670" shape**, the
tail parsing as `0x00` padding. No concurrency, no re-entrancy; CRC agrees
because save and load share the bloated length.

`CONFIG_CHUNK_SIZE = 200`, `MAX_CONFIG_CHUNKS = 20`, `MAX_CONFIG_SIZE = 2048`
(`opendisplay_constants.h:27-29`, `opendisplay_config_storage.h:7`).

---

## Diagnostics to confirm (priority order)

1. **Firmware log at `saveConfig` time:** print `total_size` vs `received_size`
   (and `chunk_number`). If `received_size != total_size`, the extra length is
   written here — a pure write/provisioning bug, unrelated to the read-time
   crash surface.
2. **Client-side config-write log:** the `total` prefix vs the byte count
   actually sent for a config write. Confirms whether the client overshoots.
3. **Firmware log inside `handle_config_read`:** `hdr.data_len`, chunk-0 prefix,
   per-chunk `chunk_number`/`chunk_size`, total sent, and whether it is entered
   once or twice per connection.
   - prefix == data_len == received, entered **once**, no leftover → write-path
     over-size (Part 4). **Not concurrency.**
   - received > prefix, entered **twice**, or leftover chunks → extra-stream /
     duplicate-delivery (Part 1 / transport).
4. **Encryption A/B:** identical interrogate with `encryption_enabled=0` vs on;
   compare received-byte and stale-notification counts.

## Remediation

**Firmware (Firmware_Silabs) — removes the trigger:**

1. In `handle_config_chunk`, **clamp `received_size` to `total_size`** (or reject
   `received_size != total_size`) before `saveConfig`, so an over-long write can
   never persist an inflated `data_len`.
2. Bound `handle_config_read` (and/or the client) to the **inner wrapper
   length**, not the stored `data_len`, as defense-in-depth.
3. Tighten the plaintext-during-session policy to an explicit allow-list rather
   than the `< 31`-byte size threshold, closing the raw-dispatch hole in
   `on_pipe_write`.

**Client (py-opendisplay) — makes the failure non-fatal (from the prior note):**

4. Route `read_firmware_version()` through `_write()`/`_read()` so it drains +
   decrypts + handles short frames like every other post-auth command.
5. Make the firmware-version read echo-tolerant (skip a non-`0x0043` frame and
   re-read, bounded) instead of raising.
6. Truncate the reassembled config to the declared length after the loop
   (`tlv_data = tlv_data[:total_length]`) so the parser never sees the padding
   tail.

**Home Assistant integration — serialize onboarding (see Part 1):**

7. Introduce a **domain-scoped, per-address BLE lock** in `hass.data[DOMAIN]`
   (keyed by normalized MAC), acquired by the config-flow probe,
   `async_setup_entry`, and reused as `runtime_data.ble_lock`. This exists
   before any entry, so it serializes probe→setup and concurrent flows —
   removing the *overlap* class even though it does not reduce the *number* of
   interrogations.
8. Cheap complement: flip `async_step_user` to
   `async_set_unique_id(address, raise_on_progress=True)` to dedupe user vs
   discovery flows.

## Source references

- HA: `custom_components/opendisplay/config_flow.py`
  (`_async_test_connection` 145-168, `async_step_user` 213-269,
  `async_step_encryption_key` 292-320),
  `__init__.py` (`OpenDisplayRuntimeData.ble_lock` 78, `async_setup_entry`
  195-356), `delivery.py` (`_drain_once` 302-340, `_drain_resync` 363-381),
  `coordinator.py` (`_check_reboot_flag` 151-171).
- py-opendisplay: `src/opendisplay/device.py` (`__aenter__` 532-585,
  `interrogate` 937-1011, `read_firmware_version` 1013-1039, `_read` 705-738).
- Firmware_Silabs: `opendisplay_pipe.c` (`handle_config_read` 724-789,
  `handle_config_write` 791-865, `handle_config_chunk` 867-910,
  `on_pipe_write` 1181-1239), `opendisplay_config_storage.c`
  (`saveConfig` 59-88, `loadConfig` 90-139, `calculateConfigCRC` 44-58),
  `opendisplay_config_storage.h` (struct 9-15), `opendisplay_constants.h`
  (`CONFIG_CHUNK_SIZE`/`MAX_CONFIG_CHUNKS` 27-29, `MAX_RESPONSE_DATA_SIZE` 30),
  `opendisplay_ble.c` (`opendisplay_ble_reload_config_from_nvm` 1496),
  `opendisplay_config_parser.c` (`loadConfig` call 523).
