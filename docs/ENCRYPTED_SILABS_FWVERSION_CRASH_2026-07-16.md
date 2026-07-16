# Encrypted-Silabs crash: root-cause analysis (2026-07-16)

Working investigation note (not user docs). Triggered by a crash reported against
an **encryption-enabled EFR32BG22 (Firmware_Silabs)** device: the client
disconnects immediately after "Reading firmware version" during interrogation.
Failure only reproduces when device encryption is enabled.

## Verdict up front

The crash is a **two-sided interaction**; encryption is the trigger, not the
direct fault.

> **STATUS UPDATE (2026-07-16): firmware confirmed correct — it is not the
> trigger.** Point 1 below originally read "Firmware_Silabs (trigger): the
> encrypted config-read over-sends." Hardware testing confirms the firmware emits
> exactly `data_len` per command and does **not** over-send. The stray/duplicate
> config frames that pollute the queue are injected by the **BLE transport**
> (BlueZ / bleak / ESPHome Bluetooth proxy) carrying the larger encrypted
> notifications, and are silently concatenated by py-opendisplay's
> chunk-number-blind reassembly. The client-side fatal amplifier (point 2) is
> unchanged and remains the actual fix target. See
> `Firmware_Silabs/docs/CONFIG_READ_OVERSEND_TRIGGER_2026-07-16.md`.

1. **BLE transport (trigger, was "Firmware_Silabs"):** duplicate `RESP_CONFIG_READ`
   notifications reach the client — it receives **670 bytes for a declared 613**
   and then finds **2 leftover "stale" notifications** still queued. Those
   leftovers are encrypted config-read frames the transport duplicated, not frames
   the tag over-emitted.
2. **py-opendisplay (fatal amplifier):** `read_firmware_version()`
   (`device.py:1013-1039`) is the **only** post-authentication command that
   bypasses the encrypt/decrypt wrappers (`self._write` / `self._read`) and reads
   the raw queue. When a stray encrypted config frame lands in the queue, it is
   handed to `parse_firmware_version()`, fails the echo check, raises
   `InvalidResponseError`, and the caller (`delivery.py:_drain_resync`,
   `__init__.py:243`) tears the link down. That is the "Disconnecting" 25 ms after
   "Reading firmware version" with no success line.

So: the trigger is the **transport** (not the firmware) and the client turns it
into a hard failure. Encryption is why it only reproduces with an encrypted
device — the larger encrypted notifications are what the transport duplicates,
leaving the queue polluted precisely in the encrypted path.

### An earlier (wrong) hypothesis, for the record

An initial guess was that the firmware *rejects* the plaintext firmware-version
command with a `0xFE` frame. Reading the actual firmware, that is wrong: the
firmware **deliberately accepts and force-plains** the firmware-version exchange.
`dispatch()` (`opendisplay_pipe.c:1067-1084`) only enforces "must be encrypted"
for frames **>= 31 bytes** (`on_pipe_write`, line 1225). A 2-byte `0x0043`
request is under that threshold and `session_alive()` is true, so it dispatches
and replies. `pipe_send()` (line 532-533) lists `RESP_FIRMWARE_VERSION` in
`force_plain`, so the reply is intentionally plaintext. The plaintext
firmware-version handshake is *by design*.

## The full encryption chain (as actually implemented)

**Key derivation & mutual auth** (client `authenticate()` at
`device.py:740-791` <-> firmware `authenticate_handle()` at
`opendisplay_pipe.c:581-680`):

1. Client -> `0x0050` step-1 (payload `0x00`). Firmware generates a 16-byte
   `server_nonce`, returns it plus a 4-byte `device_id` derived from
   `SYSTEM_GetUnique()`.
2. Client generates `client_nonce`, computes
   `challenge = AES-CMAC(master_key, server_nonce || client_nonce || device_id)`,
   sends step-2 (`client_nonce || challenge`).
3. Firmware recomputes the CMAC; on match it derives `session_key` (CMAC-based
   KDF -> AES-ECB final step, `derive_session_key` at line 192-198) and
   `session_id = CMAC(session_key, client_nonce || server_nonce)[:8]`, then
   returns a **server proof** CMAC.
4. Client derives the same `session_key`/`session_id` and verifies the server
   proof in constant time (`device.py:782-784`). Both sides zero their counters:
   client `self._nonce_counter = 0`, firmware `s_session.nonce_counter = 0`,
   `last_seen_counter = 0`.

**Per-frame format (both directions):**
`[cmd:2][nonce_full:16][ciphertext][tag:12]`, AES-128-**CCM**, tag 12 B, 13-byte
CCM nonce = `nonce_full[3:16]`, AAD = the 2 cmd bytes, plaintext = `[len:1][payload]`.
`nonce_full = session_id(8) || counter_be(8)`.

**Two independent counters:**

- **Client->device** uses `self._nonce_counter`, incremented in `_encrypt_frame`
  (`device.py:646-660`). The firmware validates it with a **+/-32 sliding replay
  window** (`nonce_replay_check`, `opendisplay_pipe.c:362-393`) and only advances
  the window **after** the CCM tag verifies (`nonce_replay_advance`, line
  397-405) — the recent `ec0bb25` fix. Three integrity failures ->
  `clear_session()`.
- **Device->client** uses `s_session.nonce_counter`
  (`encrypt_response_payload`, `opendisplay_pipe.c:470-499`). The **client does
  not track or verify** the response counter — it just reads the nonce out of
  each frame (`decrypt_response`, `crypto.py:122-149`). So response replay/reorder
  is not detected client-side.

**Plaintext exceptions during a live session** (firmware `force_plain`):
`RESP_AUTH_REQUIRED`, `0xFF` errors, `RESP_AUTHENTICATE`, `RESP_FIRMWARE_VERSION`,
`RESP_MSD_READ`. Correspondingly, the client's `_read()` (`device.py:705-738`)
only attempts decryption when a frame is **>= 31 bytes**, otherwise passes short
frames through and special-cases `0xFE`/`0xFF`. `read_firmware_version` is the
outlier that skips `_read` entirely.

**Transport quirk:** the notification queue has no request/response correlation,
so `drain_notifications()` (`connection.py:314-333`) discards whatever is queued
*before* each write. This is best-effort and races against frames still in flight.

## The anomaly that triggers it (needs one capture to fully pin)

The interrogation numbers are internally inconsistent and are the primary lead:

- Chunk-0 length prefix and the inner TLV wrapper **both declare 613**
  (`config_parser.py:72`), yet the client accumulates **670** payload bytes
  across 7 chunks (94 + 6*96).
- Only **4 real TLV packets = 128 bytes** parse; byte 128 onward is `0x00`
  padding ("Unknown packet type 0x00 at offset 128"). So the *semantic* config is
  ~133 bytes, the *declared* blob is 613, and the *delivered* stream is 670.
- Then **2 more encrypted config frames remain queued** and are drained before
  the firmware-version write.

The firmware's `handle_config_read()` (`opendisplay_pipe.c:724-789`) loop sends
`min(remaining, max_data)` with `remaining = config_len`, which *cannot* exceed
`config_len` in a single pass. The 670 > 613 therefore points to one of:
(a) a **stored blob larger than its own wrapper length** (config-storage padding
— `loadConfig` returns `hdr.data_len` that includes trailing bytes), or
(b) `handle_config_read` being **entered more than once** (retransmit /
re-trigger) so a second stream's opening chunks are read as continuation and then
as "stale." Static reading cannot distinguish these; a byte-level capture will.

## Alternate causes considered

- **Firmware rejects plaintext `0x0043` with `0xFE`** — ruled out; firmware
  force-plains it.
- **Response-counter / replay desync** — unlikely as the direct cause: the client
  does not check the response counter, and the extra config frames are
  device->client (no RX replay involvement). Worth confirming the firmware's
  `s_session.nonce_counter` isn't overflowing a byte anywhere (the 1-byte
  `payload_len` field in `encrypt_response_payload` is safe only for <=255-byte
  payloads — fine for config chunks, but flag it).
- **CCM tag failure on a config chunk** — ruled out: interrogation produced a
  valid config, so decryption succeeded for the frames it read.
- **`session_alive()` false at firmware-version time** — possible but unlikely
  (activity refreshed ms earlier); if true, the firmware would send a 3-byte
  `[00,43,FE]` which *also* crashes `parse_firmware_version` (len < 5). Either way
  `read_firmware_version` is the fragile point.
- **Genuine read timeout** — ruled out by timing: `TIMEOUT_ACK = 5.0s`, but the
  disconnect is **25 ms** after the write, i.e. an immediate raised exception.

## Remediation plan

**Client (py-opendisplay) — makes the failure non-fatal:**

1. Route `read_firmware_version()` through `self._write()` / `self._read()` like
   every other post-auth command, so it participates in drain + decrypt and
   short-frame handling. (Low risk; `_read` returns `cmd_echo(2)+payload`, exactly
   what `parse_firmware_version` expects.)
2. Make the firmware-version read **echo-tolerant**: if the frame's command code
   isn't `0x0043`, skip it and read again (bounded retries) instead of raising —
   so a stray config frame is discarded, not fatal.
3. Truncate the reassembled config to the declared length after the loop
   (`tlv_data = tlv_data[:total_length]`) so the parser never sees the padding
   tail; keep it as defense-in-depth even after the firmware fix.

**Firmware (Firmware_Silabs) — removes the trigger:**

4. Confirm and fix why `handle_config_read` emits > `config_len` bytes / extra
   frames. If it's storage padding, bound the stream to the **inner wrapper
   length**, not the physical NVM3 object size. If it's re-entrancy, guard against
   a second concurrent config-read stream per connection.
5. Consider tightening the plaintext-during-session policy: today any <=30-byte
   plaintext command is dispatched mid-session. That's intended for
   FW-version/MSD, but it's worth an explicit allow-list rather than a size
   threshold.

**Cross-repo:** the same `read_firmware_version` shape and the same `force_plain`
list exist in **Firmware_NRF54** (`opendisplay_pipe.c:571-572`); the client fix
covers all targets — apply it once, and check whether NRF54's config read shows
the same over-send.

## Diagnostics to capture next (priority order)

1. **Firmware RTT/UART log inside `handle_config_read`:** print `config_len`,
   `hdr.data_len`, the inner wrapper length, per-chunk `chunk_number`/`chunk_size`,
   and total bytes sent. Settles 613-vs-670 and storage-padding-vs-re-entry.
2. **Client-side raw frame dump:** temporarily log `len(raw)` and `raw[:4].hex()`
   for every notification in `read_firmware_version`'s read path (and the drained
   ones) to confirm the "stale" frames are `[00, RESP_CONFIG_READ, ...]` encrypted
   config chunks.
3. **Encryption on/off A/B:** run the identical interrogate on the same device
   with `encryption_enabled=0` and compare received-byte count and
   stale-notification count. If plaintext shows 613/0-stale and encrypted shows
   670/2-stale, that localizes the over-send to the encryption path.
4. **Full HA traceback:** the pasted log stops at the disconnect — the actual
   exception type/stack from `read_firmware_version`'s caller would confirm
   `InvalidResponseError` vs. a timeout in one line.

## Source references

- py-opendisplay: `src/opendisplay/device.py`
  (`interrogate` 936-1011, `read_firmware_version` 1013-1039, `_read` 705-738,
  `_write`/`_encrypt_frame` 646-675, `authenticate` 740-791),
  `src/opendisplay/crypto.py` (`decrypt_response` 122-149),
  `src/opendisplay/protocol/config_parser.py` (`parse_config_response` 53-90),
  `src/opendisplay/protocol/responses.py` (`parse_firmware_version` 222-),
  `src/opendisplay/transport/connection.py` (`drain_notifications` 314-333).
- Firmware_Silabs: `opendisplay_pipe.c`
  (`handle_config_read` 724-789, `dispatch` 1067-1179, `on_pipe_write` 1181-1239,
  `pipe_send` 514-546, `encrypt_response_payload` ~470-500,
  `decrypt_encrypted_payload` 407-457, replay window 357-405,
  `authenticate_handle` 581-680), `opendisplay_config_storage.c`
  (`loadConfig` 90-139), `opendisplay_constants.h` (`MAX_RESPONSE_DATA_SIZE`).
