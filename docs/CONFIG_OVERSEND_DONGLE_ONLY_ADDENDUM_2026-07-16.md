# Config oversend is dongle-only: the duplicate-notification mechanism (2026-07-16)

Addendum to `CONFIG_OVERSEND_ROOT_CAUSE_SYNTHESIS_2026-07-16.md`. New field
evidence: **the bug reproduces only when HA connects through a local Bluetooth
dongle (BlueZ direct) and never through an ESP32 `bluetooth_proxy`.** This
refutes the synthesis's #1 root cause (ESP32 proxy duplicate frames) and, on
re-grounding, points to a specific, source-confirmed BlueZ-only mechanism.

Source read directly (classifier flapping blocked agents; verified by hand):
bleak BlueZ backend at
`py-opendisplay/.venv/.../bleak/backends/bluezdbus/{manager.py,client.py}`;
py-opendisplay `transport/connection.py` (branch `feat/melody-notation @ 22f5319`
≡ pinned `v7.12.0`); Firmware_Silabs `opendisplay_pipe.c` (`main @ 7efd063`).

## Verdict

The proxy is exonerated; the firmware is exonerated; **the extra frames are
delivered by the client's BlueZ backend, which delivers every GATT notification
twice whenever a duplicate "device watcher" is registered for the MAC.** The
ESP32 proxy uses a different bleak backend that does not share that watcher
registry, so it delivers each notification once — hence dongle-only.

## Why the firmware and proxy are not the source

- **Firmware cannot over-emit, regardless of transport.** `handle_config_read`
  streams exactly `config_len` in one pass; the sole emitter `pipe_send_raw`
  (`opendisplay_pipe.c:508`) **drops** a frame on a busy/`!OK` status with no
  resend (`:509-511`) — that is under-send, not over-send; the pipe uses
  notifications, not indications, and `characteristic_status` (`:1271-1282`) only
  tracks CCCD on/off (no confirmation-driven resend). A small-MTU dongle that
  forced ATT long writes would hit the opcode filter at `:1192-1194` (prepared
  writes dropped) and *lose* the command (timeout), not over-read. And the
  observed clean 94/96-byte chunk sizing proves the failing dongle negotiated a
  large-enough MTU that nothing fragmented. So the firmware behaves identically on
  both transports.
- **The proxy delivers once.** The ESP32 path (`bleak_esphome` / aioesphomeapi)
  routes notifications per-connection over protobuf; it does not touch the BlueZ
  manager described below.

## The BlueZ-only mechanism (corrected)

> **Correction (this section was initially overstated).** "Two watchers on the
> device ⇒ your queue is double-filled" is **not** generally true. bleak's
> per-device watcher dispatch (`manager.py:1162-1172`) does iterate *all* watchers
> for a device, BUT each watcher's `on_value_changed` closure looks up
> **its own client's** `_notification_callbacks` (`client.py:187-191`). Two
> watchers on two *different* `BleakClient`s therefore feed two *different* queues
> — a leaked orphan watcher delivers into a dead (unread) queue and does **not**
> duplicate frames in the live connection. Double-delivery to one live queue
> requires two feeds into the **same** `_notification_callback` bound method,
> which narrows the mechanism to the one below.

The facts that hold:

1. **bleak's `BluezManager` is a process-global singleton** with a per-device
   `set` of watchers, dispatched on each `Value` `PropertiesChanged`
   (`manager.py:1162-1172`). The ESP32 proxy uses a different backend and does not
   share this manager — so any BlueZ-manager-mediated duplication is dongle-only.
2. **`start_notify` registers the callback into `self._notification_callbacks`
   BEFORE issuing the `StartNotify` D-Bus call** (`client.py:979` then `:980`), so
   a `start_notify` that raises still leaves the callback registered on that
   client.
3. **The one path that double-feeds a single live queue: OpenDisplay's cache-retry
   loop** (`connection.py:107-137`). `_attempt_connect` sets `self._client`
   (`:182`) then calls `_setup_notifications` → `start_notify` with the **same
   bound `self._notification_callback`** (feeding the **same** `_notification_queue`,
   created once per `OpenDisplayConnection`). If an attempt registers that callback
   on client_A and then fails on a stale-GATT error, and the following
   `_clear_cache_and_drop`/`disconnect` does **not** cleanly remove client_A's
   watcher (`disconnect()` can raise and is swallowed at `connection.py:207-210,
   219-220`, so bleak's `_cleanup_all`/watcher-removal never runs), the retry's
   client_B registers the *same* callback. Now client_A's and client_B's watchers
   both route to the one `_notification_queue` → every notification enqueued twice.
   This needs (a) the retry to actually fire (a GATT-cache/"invalid handle"
   mismatch — BlueZ keeps its own GATT cache, so it can) and (b) a disconnect that
   failed to remove the first watcher. It is BlueZ-only on both counts.
4. **bleak also documents a distinct StartNotify duplication hazard**
   (`client.py:947-953`): in the default StartNotify mode, doing a **GATT read on
   the notifying characteristic** makes BlueZ return the value as *both* a
   notification and a read — duplicating it. Relevant only if a code path reads the
   pipe characteristic (OpenDisplay appears to use notify-only; flag to verify).

**Status: the duplication is localized to the BlueZ client path (mechanism #3 is
the concrete candidate), but which trigger fired in the field run is not provable
from static reading — it needs the capture below.** The `read`+`StartNotify`
double (#4) and a leaked-but-same-queue watcher (#3) are the live candidates; a
leaked *different*-client watcher is ruled out (feeds a dead queue).
## Byte math: duplication reproduces the shape, but the *rate* matters

Firmware chunks C0(94+prefix), C1..C5(96), C6(39). If notifications duplicate, the
`[2:]` of a duplicated frame is 96 bytes (even a duplicated C0: its 2-byte
length-prefix becomes payload), so every extra frame adds a uniform +96.

- **Every-frame-doubled** (mechanism #3, if the retry fired) → queue
  C0,C0,C1,C1,…; `interrogate` consumes C0,C0dup,C1,C1dup,C2,C2dup,C3 →
  `94→190→286→382→478→574→670`, stop. Reproduces the exact increments and the full
  96-byte 7th chunk. **But** it leaves ~7 frames queued (C3dup,C4,C4dup,…), whereas
  the log drained only ~2 before fw-version — so full doubling *over-predicts*
  leftovers.
- **One extra frame per exchange** (a single stray/duplicated config frame inserted
  mid-stream) → also lands the 7th consumed chunk on a full 96 and reaches 670,
  and leaves ~1 frame queued — a **closer fit** to the observed 1/1/2 stale counts
  and the steady "~1 per exchange" pattern.

Both reproduce the 670 signature; the low observed leftover count favors a
**low-rate** duplication (≈1 extra per response), not wholesale doubling of every
frame. Either way it is impossible on the single-delivery proxy path. The exact
rate — and therefore which mechanism — is what the capture must settle.

## What changes, and what does not

- **Refuted:** synthesis R1 (ESP32 proxy duplication). Proxy and firmware are both
  cleared.
- **New R1:** duplicate notification delivery in the BlueZ client path (leaked
  same-queue watcher via the cache-retry loop, mechanism #3, and/or the
  read+StartNotify hazard #4) — localized to BlueZ, exact trigger pending the
  capture.
- **Unchanged:** R2 (client fragility) is still the fatal amplifier, and **the
  remediation is unchanged and now even better targeted:**
  - **P2 (primary fix) neutralizes double-delivery at the source**: validate
    `chunk_number` in `interrogate` and **skip a frame whose number repeats or
    regresses** (a doubled Cᵢ carries the same `chunk_number` → dropped), truncate
    `tlv_data` to `total_length`, and drain after the loop. This makes the bug
    non-corrupting regardless of how many watchers fire.
  - **P1** (echo-tolerant `read_firmware_version`) still makes any leftover
    non-fatal.
- **Newly elevated — two BlueZ-targeted fixes:**
  - **Do not swallow a failed disconnect silently / guarantee watcher cleanup.**
    In `connection.py` disconnect paths, if `self._client.disconnect()` raises,
    the client's cleanup may not have run; at minimum log loudly, and prefer
    calling the backend cleanup / recreating state so a watcher can't leak. (The
    real leak is inside bleak, but not leaning on best-effort disconnect reduces
    it.)
  - **The per-MAC connection lock (P6) is now more strongly justified.** On BlueZ,
    overlapping/rapid-successive connections to one MAC are a concrete *trigger*
    for the duplicate-watcher condition — not merely a GATT-interleave risk. A
    domain-scoped, pre-entry per-address lock (see the lock decision memo)
    serializes probe → setup and the dual-flow window, preventing two live
    BlueZ clients (hence two watchers) from coexisting for the same device.

## Deciding capture (one run, client-side, no reflash)

Log each received notification's first bytes + `chunk_number` in `interrogate`.
**Back-to-back identical `chunk_number`/bytes = duplicate delivery confirmed
(watcher leak).** Complement: count entries in the bleak BlueZ manager's
`_device_watchers[device_path]` at notify time (temporary instrument) — a count of
2 is the smoking gun. An `encryption_off` A/B is no longer needed to localize the
transport; a proxy-vs-dongle A/B already did.
