# Config-read oversend / encrypted-Silabs fw-version crash — root-cause synthesis (2026-07-16)

Working investigation note (not user docs). Synthesizes and adjudicates the four
prior analyses:

- `Firmware_Silabs/docs/CONFIG_READ_OVERSEND_ANALYSIS_2026-07-16.md`
- `Firmware_Silabs/docs/CONFIG_READ_OVERSEND_TRIGGER_2026-07-16.md`
- `Home_Assistant_Integration/docs/CONFIG_READ_OVERSEND_HA_ANALYSIS_2026-07-16.md`
- `Home_Assistant_Integration/docs/ENCRYPTED_SILABS_FWVERSION_CRASH_2026-07-16.md`

Every claim below was re-grounded in local source by three independent
high-effort review agents (one per repo). **Source versions audited:**

| Repo | Branch | HEAD |
|---|---|---|
| `Firmware_Silabs` | `main` | `7efd063` ("fix streaming compression flag") |
| `py-opendisplay` | `feat/melody-notation` | `22f5319` — **byte-identical to the pinned release `v7.12.0`** for `device.py`, `config_parser.py`, `transport/connection.py` (verified `git diff v7.12.0` empty), so the audited client code is exactly what HA runs |
| `Home_Assistant_Integration` | `feat/play-melody` | `d30f77a` (manifest pins `py-opendisplay[silabs-ota]==7.12.0`) |
| `Firmware_NRF54` (cross-check) | `main` | `635d7d2` |

Incident recap (encryption-enabled EFR32BG22, `F0:FD:45:F1:F6:93`): interrogate
logged `total_length = 613`, accumulated **670** bytes (94, then +96 × 6), only
128 bytes of TLV parsed before `0x00` padding, stale notifications drained
1 (auth) / 1 (before read-config) / 2 (before fw-version), then
`read_firmware_version` failed ~25 ms after its write and HA disconnected.

---

## 1. Corrections to the prior documents (source-proven)

These findings change the conclusions of all four docs and must be read first.

### C1. "total_length = 613" is the **stream prefix only** — the docs conflated two fields
The logged value comes from `device.py:964`
(`total_length = int.from_bytes(chunk_data[2:4], "little")`, logged at `:967`)
— the chunk-0 **stream length prefix**. The inner TLV **wrapper** length is a
different field parsed at `config_parser.py:72` and logged only at DEBUG
("TLV wrapper: length=%d"). The crash doc and the Silabs ANALYSIS doc both
asserted "prefix **and** wrapper declare 613"; **the wrapper's value is not
established by the incident log at all.** Inferences built on "wrapper == 613"
(notably ruling the write-path bug in or out on that basis) were unsupported.

### C2. The "full 96-byte 7th chunk proves a second stream" inference is wrong
The client's loop is `while len(tlv_data) < total_length` (`device.py:974`),
stopping at the **first cumulative ≥ 613**, which lands on 94 + 96×6 = **670**
whenever the 7th consumed frame is full-size. The client never *requires* a
39-byte tail. The ANALYSIS doc's core inference (full 7th chunk ⇒ interleaved
second stream) is not forced.

### C3. 670 is where the **client stopped**, not what the firmware sent
The firmware demonstrably sent **≥ 10** config frames: 7 consumed + 2 drained
before fw-version + ≥ 1 arriving in the drain race window (the frame that
crashed the parse). All four docs reasoned about "670 delivered"; the true
stream was ≥ ~860 bytes' worth of frames. The docs undercounted.

### C4. The firmware emits **no** per-exchange extra notification
Every `pipe_send` call site was enumerated: each inbound command produces
exactly one response frame (the sole two-frame site,
`handle_direct_write_end`, is not in the interrogation flow), and the only
notification emitter in the whole firmware is `pipe_send_raw` →
`sl_bt_gatt_server_send_notification` (`opendisplay_pipe.c:508`) with **no
retry/resend on failure** (frame dropped, never duplicated). The TRIGGER doc's
"firmware is emitting ~1 extra notification per exchange" is contradicted; the
steady stale residue must originate **above the firmware** (transport).

### C5. Conjecture A (long-write double dispatch) is structurally refuted
`on_pipe_write` (`opendisplay_pipe.c:1181-1239`): `offset != 0` fragments are
buffered and `return` **without dispatch** (`:1208`); `offset == 0` dispatches
*either* the reassembled buffer *or* the direct data, never both; and
prepare/execute opcodes are filtered at `:1192-1194`. No double-dispatch path
exists — independent of MTU arguments.

### C6. HA never writes config; our client never pads
`write_config` (`py-opendisplay device.py:1279`) has **zero call sites** in
`custom_components/opendisplay/`. And `build_write_config_command`
(`commands.py:542, 550-553`) sends exactly `total_size` bytes, last chunk
unpadded — in every historical version (`git log -L`: only 40835a9's 198→200
first-chunk fix). So the HA-doc Part 4 "leading root cause" (a padded client
write persisting inflated `data_len`) **cannot have been triggered by our
stack** — and independently, a stored `data_len = 670` would make the read
prefix 670 (same variable), contradicting the logged 613.

### C7. "Route `read_firmware_version` through `_write`/`_read`" does **not** fix the crash
A leftover encrypted config frame decrypts fine in `_read` and returns the
config-read echo — `parse_firmware_version` still fails its
`echo != 0x0043` check (`responses.py:240-242`) and still raises
`InvalidResponseError`. Also, `read_firmware_version` **already drains**
(via `write_command`'s default `drain_stale=True`, `connection.py:364-365`) —
the crash frame arrived *after* that drain, in the race window of the
synchronous, non-awaiting drain (`connection.py:314-333`). Only **echo
tolerance** (skip-and-re-read) genuinely fixes this surface.

### C8. The field device must be running ~today's firmware tip
`MAX_CONFIG_SIZE` was **512 until commit `1b12a90` (2026-07-16, today)**, and
`loadConfig` rejects `data_len > MAX_CONFIG_SIZE` (`config_storage.c:118`). A
613-byte config is unreadable on any older build. Therefore the audited
`main @ 7efd063` code is the code on the failing device, and its read loop
(prefix == budget, single variable, `opendisplay_pipe.c:742-786`) genuinely
cannot over-send in one pass.

---

## 2. Byte-accounting elimination (which mechanism fits the log?)

A clean 613-byte stream is frames of payload sizes `[94+prefix, 96×5, 39]`.
The client consumed 7 frames: one 94-byte first chunk and **six** 96-byte
continuations — i.e. at least one consumed 96-byte frame was **not** part of a
single clean stream. Candidate mechanisms:

| Mechanism | Prediction | Verdict |
|---|---|---|
| **S1** single clean stream, `data_len=613` | 7th chunk = 39 B, stop at 613, 0 leftovers | **Excluded** (log shows +96 7th chunk, 670, ≥3 leftovers) |
| **S2** single stream, stored `data_len=670` (write-path over-store) | prefix/logged total_length = **670** | **Excluded** (logged 613); also unreachable by our clients (C6) |
| **S3** single stream running past its prefix | needs firmware where prefix ≠ budget | **Excluded on this build** (C8: prefix==budget is structural) |
| **S4a** second stream, **interleaved** | possible 670 pattern | **Excluded**: Silabs dispatch is strictly serialized — the 7-chunk loop runs synchronously inside the single `sl_bt` event handler and never yields (`opendisplay_pipe.c:754-788, 1258-1270`); two reads run back-to-back, never interleaved |
| **S4b** second stream, **sequential** (A0..A6 then B0..B6) | FIFO ⇒ client consumes A6 (39 B) as its 7th frame → stops at exactly **613**, not 670 | **Excluded** by the logged 670 |
| **S5 duplicate notification delivery at the transport layer** | a duplicated frame's `[2:]` payload is 96 B (even a chunk-0 dup: 2 prefix + 94 data = 96) → uniform +96 increments; client stops at 670; remaining real frames + dups stay queued as "stale"; identical mechanism explains the ~1-stale-per-exchange pattern all session (auth, pre-read-config) | **Only mechanism consistent with every observed fact** |

Note the elegant detail in S5: *any* duplicated frame — including chunk 0 —
contributes exactly +96 after the client strips the 2-byte chunk number,
matching the perfectly uniform increments in the log. The observed leftover
count (≥3 during the config exchange, plus singles during earlier exchanges)
implies several duplications this session, consistent with a misbehaving
transport hop rather than a firmware defect.

---

## 3. Root-cause findings, ranked most → least likely

### R1 — Transport-layer duplicate notification delivery (most likely proximate cause)
Confidence: **high** that the extra frames are link-level duplicates of real
frames (only surviving mechanism, §2); **medium** on which hop duplicates.
Suspects in order: (a) **ESP32 `bluetooth_proxy` re-delivery** (the deployment
uses a proxy; disconnect/queue coalescing there is already known to lag),
(b) HA bleak/backhaul duplication, (c) Silabs host-stack re-notify below the
application (no host-visible mechanism exists in our code — C4).
Duplicated encrypted frames carry **identical nonce counters** (replays); the
client never checks response-nonce monotonicity (`crypto.py:122-149`), so dups
decrypt and concatenate silently.
*Caveat:* if the field device were running a non-tip custom build (against the
C8 inference), S3 re-enters; the confirming capture (§5) distinguishes them in
one run either way.

### R2 — py-opendisplay client fragility: the certain, code-proven fatal amplifier
Not "likely" — **certain**, and it is what turns benign duplicates into a
bricked setup:
1. `interrogate()` ignores `chunk_number` entirely (`device.py:983-989`) — a
   duplicate/foreign frame is blindly appended.
2. Stops at first cumulative ≥ `total_length`, **never truncates**
   (`device.py:974, 989, 1000`) — over-length tail reaches the parser
   ("Unknown packet type 0x00 at offset 128").
3. **No drain after the loop** — surplus frames sit queued and poison the next
   command.
4. `read_firmware_version` bypasses `_write`/`_read` (`device.py:1024-1027`)
   and `parse_firmware_version` hard-raises on any non-0x0043 echo
   (`responses.py:240-242`) with no tolerance/retry — one stray frame ⇒
   `InvalidResponseError` ⇒ HA disconnect.
5. HA then converts this into a **retry-hammer loop**: `__init__.py:260-270`
   catches it and raises `ConfigEntryNotReady` (non-sleepy device ⇒ reconnect,
   re-interrogate, re-crash on backoff).

### R3 — Silabs `<31-byte` plaintext dispatch hole + missing config-read re-entrancy guard (real, latent — not this incident)
`on_pipe_write:1225` only decrypt-gates frames ≥ 31 B; shorter frames dispatch
**raw** during a live session (`:1238`), and `dispatch` only rejects when
`!session_alive()` (`:1076`). A plaintext 2-byte `CMD_CONFIG_READ` — or
`CMD_REBOOT` / `CMD_DEEP_SLEEP` — mid-session executes unauthenticated. With
no re-entrancy guard on `handle_config_read` (`:724-789`, `:1089-1090`), that
is a concrete second-stream trigger and a security hole. **NRF54 already
closed this** (`Firmware_NRF54 opendisplay_pipe.c:1367-1370`). Ruled out as
this incident's trigger only because nothing in our stack sends such a frame
on a live session.

### R4 — Silabs config-write over-store (real, latent — ruled out for this incident)
`handle_config_chunk` completes on **chunk count** and never clamps
`received_size` to the declared `total_size` (`opendisplay_pipe.c:884, 899-900`);
a padding/foreign writer can persist `data_len > total_size` (e.g. 670 from a
declared 613 via 200-byte chunks), CRC self-consistent, undetectable at rest
(`config_storage.c:71-137`). Excluded here by C6 + the S2 row (prefix would
read 670). **NRF54 already immune** (`:984, 1004-1009`).

### R5 — HA onboarding multiplicity / missing pre-entry serialization (exposure multiplier, not the byte-level cause)
Encrypted onboarding runs **two full config streams plus one rejected
plaintext probe within seconds** (`config_flow.py:159-164` → `__init__.py:238-243`),
all before `runtime_data.ble_lock` exists (`__init__.py:78, 305`). The
probe→setup handoff is *sequential* (`device.py:593-595` awaits disconnect) —
the HA doc overstated "overlap on one link" — but the
`raise_on_progress=False` manual-add flow (`config_flow.py:221`) is a genuine
dual-flow window, and every extra interrogation is another roll of the R1
dice. Explains "only reproduces with encryption" (a plaintext device streams
config once; an encrypted one twice, with more exchanges to duplicate).

### R6 — Ruled out entirely
Crypto framing/stripping errors (uniform 96-byte plaintext increments prove
correct CCM strip; a parseable config proves tag verification); firmware
read-loop over-send (C8); firmware duplicate emission (C4); long-write double
dispatch (C5); app-layer write retries (single `write_gatt_char`,
`connection.py:371-378`; `interrogate` is `@_serialized` and called once);
timeout (the 25 ms failure is a synchronous echo-mismatch raise).

---

## 4. Firm patch recommendations, most → least important

**P1 (py-opendisplay) — echo-tolerant `read_firmware_version`.**
In `device.py:1013-1039`: on a response whose command echo ≠ `0x0043`, discard
and re-read (bounded, e.g. 3 attempts / short deadline) instead of raising.
This kills the observed crash under **every** candidate mechanism, confirmed
or not, and unbricks encrypted onboarding immediately. (Do *not* rely on
re-routing through `_read` alone — C7.)

**P2 (py-opendisplay) — harden the interrogate reassembly loop.**
In `device.py:936-1011`: (a) track `chunk_number`; **skip** frames whose
number repeats or regresses (this silently absorbs transport duplicates —
the R1 mechanism — and detects a genuine second stream); (b) truncate
`tlv_data = tlv_data[:total_length]` before `parse_config_response`;
(c) `drain_notifications()` after the loop completes. Together these remove
both the corruption and the queued-leftover poisoning at the source. A
response-nonce monotonicity check in `decrypt_response` is a principled
complement (duplicates are literal replays).

**P3 (Firmware_Silabs) — close the `<31-byte` plaintext dispatch hole.**
Mirror NRF54: in `on_pipe_write`, when a session is alive, reject sub-31-byte
plaintext commands except `CMD_FIRMWARE_VERSION` (respond `0xFE`). This is a
**security fix** (plaintext `CMD_REBOOT`/`CMD_DEEP_SLEEP`/`CMD_CONFIG_READ`
execute unauthenticated mid-session today), independent of this incident.

**P4 (Firmware_Silabs) — clamp the config-write path.**
Mirror NRF54 in `handle_config_write`/`handle_config_chunk`: reject
`received_size + len > total_size`, complete on byte total
(`received_size >= total_size`), and error on `received_size != total_size`
at completion, so `data_len` can never exceed the declared length.

**P5 (Firmware_Silabs) — re-entrancy guard / debounce on `handle_config_read`.**
Per-connection in-progress flag; a repeated `CMD_CONFIG_READ` restarts the
stream rather than appending a second one. Defense-in-depth given the strictly
serialized event loop (cheap, and protects future refactors that add yields —
NRF54's retry loop already sleeps mid-stream).

**P6 (Home_Assistant_Integration) — pre-entry per-address BLE lock + flow dedupe.**
Domain-scoped `hass.data[DOMAIN]` lock keyed by normalized MAC, acquired by
the config-flow probe and `async_setup_entry`, then reused as
`runtime_data.ble_lock`; and flip `async_step_user` to
`raise_on_progress=True` (`config_flow.py:221`). Reduces onboarding connection
churn and closes the dual-flow window (R5). Also consider skipping the
redundant setup-time interrogation by carrying probe #2's config into the
entry, halving encrypted-onboarding streams.

**Priority rationale:** P1+P2 are small, self-contained, cover all targets at
once (the client is shared), and make the failure class non-fatal even before
the transport culprit is pinned; ship them first (patch release, bump the HA
manifest pin). P3 is the highest-value firmware change because it is a live
security hole. P4/P5 close proven-latent corruption paths. P6 is hygiene that
reduces exposure but fixes nothing by itself.

---

## 5. The one capture that settles R1's remaining ambiguity

Add (temporarily, client-side — no reflash needed) a DEBUG log of each
received config chunk's **`chunk_number` and the response nonce counter**
in the interrogate loop:

- **Duplicate chunk numbers / identical nonces** → transport-layer duplication
  confirmed (R1); then A/B with and without the ESP32 proxy (direct adapter)
  to pin the hop.
- **A mid-stream reset to 0 with fresh nonces** → a genuine second firmware
  stream (would point back to R3's trigger class).
- **Monotonic numbers running past chunk 6 with fresh nonces** → the S3
  "streams past prefix" case (would mean the device is not on the assumed
  build; capture its actual firmware version).

Complementary: firmware RTT invocation counter in `handle_config_read`
(enters once vs. twice), and an encryption-off A/B run.

---

## 6. Cross-repo status (NRF54)

`Firmware_NRF54 main @ 635d7d2` already has: the write-path clamp (P4
equivalent, `:984, 1004-1009`), the plaintext-hole fix (P3 equivalent,
`:1367-1370`), no long-write offset path at all, and a `-ENOMEM`
notify retry that re-sends only *unsent* frames (not a duplication source).
Only P5 (explicit config-read re-entrancy guard) and the client-side fixes
apply there. The Silabs repo is the outlier on all three firmware defects.
