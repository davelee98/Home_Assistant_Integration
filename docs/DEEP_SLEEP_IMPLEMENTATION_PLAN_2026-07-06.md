# OpenDisplay Deep Sleep — Architecture & Implementation Plan

*2026-07-06 — Companion to [DEEP_SLEEP_FINDINGS_2026-07-06.md](DEEP_SLEEP_FINDINGS_2026-07-06.md) (cross-stack behavioral findings). This document is the forward-looking design: current state, architectural considerations, component factoring, and a detailed implementation plan. Scope: ESP32 firmware variant, with changes focused on the Home Assistant integration; py-opendisplay changes where necessary; firmware changes minimized and listed separately.*

---

## 1. Current state of the integration and components

### 1.1 The device side (ESP32 firmware) — what the integration must accommodate

Validated against `Firmware/src` at HEAD:

| Behavior | Detail | Source |
|---|---|---|
| Sleep entry condition | `power_mode == 1` (BATTERY) **and** `deep_sleep_time_seconds > 0`; entered whenever BLE is idle | `main.cpp:180-198` |
| Sleep mechanics | Advertising stopped, BLE deinitialized, **timer wakeup only**, power latch held, `esp_deep_sleep_start()` | `main.cpp:275-307` |
| Wake behavior | Full CPU boot → `minimalSetup()` (config + IO + BLE advertising; no display init, no WiFi) | `main.cpp:46-51, 245` |
| Wake window | Advertises for `sleep_timeout_ms` (default **10 s** when 0); returns to sleep if nothing connects | `main.cpp:85-101` |
| On connect | `fullSetupAfterConnection()` brings up WiFi + panel driver; device stays awake while a central is connected (`bleActive`) | `main.cpp:85-90, 256` |
| First boot | 2-minute grace period before first sleep (`deep_sleep_count == 0` only) — the adoption window | `main.cpp:180-196` |
| State across sleep | `woke_from_deep_sleep`, `deep_sleep_count`, `displayed_etag` are `RTC_DATA_ATTR` and survive; `rebootFlag` does **not** (known bug — every wake advertises "rebooted") | `main.h:126, 286-295` |
| Sleep interval range | `deep_sleep_time_seconds` is uint16 → 1 s to ~18.2 h | `models/config.py:207` |
| Command 0x0052 | "Deep sleep now" handled by ESP32 and Silabs firmware; in the official protocol spec | `device_control.cpp:692`, opendisplay.org `protocol/ble-flow.html` |

Key consequence: **a sleeping ESP32 device is completely dark** — no radio, not connectable, not scannable. The only contact opportunity is the ~10 s advertising window after each timer wake. Once a central connects inside that window, the device stays awake for the whole session, so a connection established at wake time can run arbitrarily long work (uploads, OTA).

### 1.2 py-opendisplay

- **Connection layer**: `bleak-retry-connector` with `max_attempts=4`, `timeout=10 s`, plus one GATT-cache-clear retry (`transport/connection.py:33-117`). Both knobs are already exposed on `OpenDisplayDevice(timeout=…, max_attempts=…)` (`device.py:390-392`).
- **Power config**: parses/serializes `power_mode`, `sleep_timeout_ms`, `deep_sleep_time_seconds`, `deep_sleep_current_ua` (`models/config.py:196-256`). No convenience "is deep sleep enabled" predicate.
- **Config serialization**: `config_to_json()` / `config_from_json()` round-trip a full `GlobalConfig` (`models/config_json.py:68, 434`) — ready to use for caching device config in the HA config entry.
- **Advertisement model**: parses the status byte, exposing `reboot_flag` (bit 1) and the reserved `connection_requested` (bit 2) (`models/advertisement.py:384-394`).
- **Gap**: no deep-sleep command — only `reboot()` (0x000F, `device.py:912`). 0x0052 is unimplemented.

### 1.3 Home Assistant integration (`custom_components/opendisplay`)

- **Monitoring is fully passive.** `OpenDisplayCoordinator` is a `PassiveBluetoothDataUpdateCoordinator` fed only by advertisements (`coordinator.py:40-51`). Sensors (battery, temperature, RSSI, last-seen) never connect.
- **Connections happen at exactly three points:**
  1. **Entry setup** — `async_setup_entry` requires a connectable `BLEDevice` *and* a successful connect to read firmware + config; otherwise raises `ConfigEntryNotReady` (`__init__.py:97-128`).
  2. **Service calls** — `upload_image`, `drawcustom`, `activate_led`, `activate_buzzer` all go through `_async_connect_and_run`, which resolves the cached `BLEDevice` and connects immediately (`services.py:283-346`). Failure raises `upload_error` and **the content is dropped** — no queue, no retry.
  3. **OTA** — `update.py` connects directly for version checks and flashing.
- **Concurrency primitives already in place** (reused by this design):
  - Per-entry `ble_lock` serializing all BLE access to one tag (`__init__.py:58`).
  - Latest-wins upload semantics — a new upload cancels the in-flight one (`services.py:444-450`).
  - `partial_state` tracking the last-uploaded frame for differential partial refresh (`__init__.py:62`); the panel-side `displayed_etag` survives deep sleep, so partial refresh remains valid across sleep cycles.
- **Reboot handling**: coordinator detects a False→True edge on the advertised reboot flag and reloads the entire config entry, which reconnects (`coordinator.py:129-150`, `__init__.py:190-210`). Combined with the firmware's unpersisted `rebootFlag`, this is the churn-loop hazard documented in the findings doc.
- **Image entity** (`image.py`) shows the last *successfully delivered* frame, updated via dispatcher signal after upload completes (`services.py:409`). This is the natural place to represent queued-but-unsent content.
- **Config flow**: bluetooth discovery + user flow + encryption key + reauth. **No options flow exists** (`config_flow.py`).
- **`const.py`** holds only `DOMAIN`, `CONF_ENCRYPTION_KEY`, and one dispatcher signal — no option constants yet.

### 1.4 What HA core already provides (validated against `core` checkout, 2026.6 dev)

These built-in mechanisms shape the design — some help, some actively interfere:

1. **Setup retry with exponential backoff.** `ConfigEntryNotReady` → `SETUP_RETRY` state, retried at `min(2^tries × 5 s, 600 s)` (`config_entries.py:843`). Blind timer retries against a device sleeping N minutes with a 10 s window have a per-try success probability of roughly `10/N·60` — a few percent. **Timer-based retry alone is not a solution.**
2. **Advertisement-triggered reload of entries in SETUP_RETRY.** When a Bluetooth *discovery flow* fires for an already-configured entry in `SETUP_RETRY`, HA schedules an immediate reload (`config_entries.py:3158-3169`). This is the built-in "retry when the device reappears" path — **but** it only fires if the integration matcher re-triggers discovery, which only happens after the address *disappears* from the scanner history (`bluetooth/manager.py:174-176`) — i.e. after the device has been unavailable long enough to be expired. It is timing-dependent and races the 10 s wake window (reload → connect must land inside the window; scheduled reload usually does, since it fires on the wake advertisement itself). It works *sometimes* today; it is not something to build on, and extending the availability window (below) deliberately disables it.
3. **Availability tracking with a configurable horizon.** Devices are marked unavailable when advertisements go stale (fallback ~5 min, `UNAVAILABLE_TRACK_SECONDS = 300`). Critically, HA exposes **`async_set_fallback_availability_interval(hass, address, seconds)`** (`bluetooth/api.py:297`) — a per-address override of the staleness horizon. This is the sanctioned lever to keep a deep-sleeping device "available" between wakes. (The *learned* advertising-interval mechanism needs many consecutive adverts and will never learn a sleep cycle; the fallback interval is the right tool.)
4. **Advertisement callbacks.** `async_register_callback` (per-address advertisement callback) and `async_process_advertisements` (await-next-matching-advert with timeout) (`bluetooth/api.py:138, 165`). The coordinator already receives every advertisement via `_async_handle_bluetooth_event` — no new registration is needed for the wake trigger; a hook on the existing coordinator suffices.
5. **Service response data** (`SupportsResponse.OPTIONAL`) — lets `upload_image`/`drawcustom` report "delivered" vs "queued" to automations without blocking.

---

## 2. Architectural considerations

### 2.1 Design principles

**P1 — Asleep is a state, not an error.** For a device configured for deep sleep, unreachability is its normal operating condition ~99% of the time. Every code path that today treats "can't connect" as failure must, in sleep mode, treat it as "defer".

**P2 — The advertisement is the only reliable rendezvous.** All communication with a sleeping device must be initiated within seconds of seeing a wake advertisement. Everything else (setup, delivery, OTA) is architected as *work queued until the next rendezvous*.

**P3 — Never connect without a reason.** Each connection forces the device through `fullSetupAfterConnection()` (including WiFi init) and holds it awake — battery cost is dominated by connected time, not advertising. The integration must connect only when it has pending work, and must batch all pending work into a single connection per wake.

**P4 — Transport-agnostic trigger.** The rendezvous trigger is "device seen on any transport". Today that is a BLE advertisement; the future WiFi implementation (see [WIFI_ARCHITECTURE_2026-07-06.md](WIFI_ARCHITECTURE_2026-07-06.md)) delivers the same trigger from mDNS. The delivery machinery must depend on a *device-seen event*, not on BLE specifics.

**P5 — Minimize firmware changes.** Everything below works against today's firmware. Two small firmware fixes are recommended (§5.4) but nothing depends on them.

### 2.2 Key design decisions

#### D1 — How sleep mode is determined

Auto-detect from the device's own config, already held in `entry.runtime_data.device_config`:

```
sleepy = power_option.power_mode == PowerMode.BATTERY (1)
         and power_option.deep_sleep_time_seconds > 0
```

This exactly mirrors the firmware's own sleep-entry condition (`main.cpp:198`), so integration and device can never disagree. An options-flow override (`auto` / `force on` / `force off`, default `auto`) covers edge cases (e.g. Silabs Flex devices whose sleep is command-driven, or a user who wants strict-failure semantics).

#### D2 — Setup from cache: the entry must load without the device

**Problem (user scenario 1):** device asleep at HA startup → `async_setup_entry` can't connect → `ConfigEntryNotReady` → entities gone, timer retries rarely coincide with a wake window, entry effectively dead until luck strikes.

**Decision:** cache everything setup needs — serialized `GlobalConfig` (via `config_to_json`), firmware version, `is_flex` — in `entry.data` after every successful interrogation. On subsequent setups, when the device is not immediately reachable **and** the cache says it is a sleepy device, **set up entirely from cache**: build runtime data, register device info, start the passive coordinator, forward platforms — no connection at all. Mark a `config_resync_pending` flag; the delivery manager (D4) re-reads firmware/config opportunistically at the next wake and updates the cache.

Rationale for caching over smarter retries:
- It removes the race entirely instead of trying to win it. HA startup, HA restarts, and reloads all become instant and deterministic.
- The built-in rediscovery-reload path (§1.4-2) is disabled anyway once we extend the availability interval (D3): the address never expires, the matcher never resets, discovery never re-fires. Caching replaces a mechanism this design would otherwise silently break.
- It matches how HA treats other sleepy-device ecosystems (Zigbee ESLs, ESPHome deep-sleep nodes): entities exist and hold state; freshness is communicated via availability and `last_seen`.

First-time adoption still requires a live connection — acceptable, because the config flow runs during the firmware's 2-minute first-boot grace period, and a user adopting a device has it awake by definition. If setup finds no cache and cannot connect, `ConfigEntryNotReady` remains correct.

Cache invalidation: rewritten after every successful config read (setup, post-reboot resync, post-wake resync). Reauth clears nothing (the key lives separately in `entry.data`).

#### D3 — Availability policy: entities stay available across sleep

**Problem (user scenario 1a):** with the default ~5 min staleness horizon, any sleep interval > ~4 min flaps every entity unavailable/available once per cycle.

**Decision:** when sleep mode is active, call
`async_set_fallback_availability_interval(hass, address, deep_sleep_time_seconds × missed_cycles + wake_window + 60 s)`
at setup and whenever the config changes. `missed_cycles` is an options-flow setting (default **3**): the device is marked unavailable only after missing three consecutive expected wakes. Examples: 5 min sleep → unavailable after ~16 min of silence; 12 h sleep → ~36 h. This directly implements the user requirement "leave the entry alive until some defined period of unavailability (hours or days)" — with the period derived from the device's own cadence rather than a wall-clock guess, and clamped by an absolute options override for users who want a fixed horizon.

The `last_seen` sensor already reports true freshness, so nothing is hidden from the user. Availability now means "checking in on schedule" instead of "advertising right now" — the correct semantic for a sleepy device.

#### D4 — Wake-triggered delivery: queue work, deliver at the rendezvous

**Problem (user scenario 2):** sending to a sleeping device fails after ~40 s of bleak retries and drops the content.

**Decision:** introduce a per-entry **delivery manager** owning *pending work slots*, triggered by the coordinator's advertisement handler:

- **Pending slots, latest-wins per type** (consistent with existing upload semantics):
  - `pending_upload`: the *prepared* image (post dither/encode/compress — CPU work done at queue time, once), refresh mode, partial state reference, plus the preview JPEG, `created_at`, `expires_at`, `attempts`.
  - `pending_config_resync`: flag — re-read firmware + config, refresh cache (set by D2 cache-setup and D6 reboot handling).
  - `pending_ota`: deferred to the OTA flow itself (§4, Phase 3); the slot exists so a wake can resume a user-requested update.
- **Trigger:** `OpenDisplayCoordinator._async_handle_bluetooth_event` already fires on every advertisement. Add a `async_subscribe_device_seen` hook (mirroring the existing `async_subscribe_reboot` pattern, `coordinator.py:62-74`). The delivery manager subscribes; on device-seen with pending work and no delivery in flight, it starts a delivery task **immediately** — every millisecond of the 10 s window counts.
- **Single connection per wake (P3):** the delivery task acquires `ble_lock`, opens one `OpenDisplayDevice` session, and drains *all* pending slots in priority order: upload first (user-visible), then config resync (cheap reads on the already-open link), then OTA. The device stays awake while connected, so the window only constrains connection establishment, not the work.
- **Failure handling:** if a delivery attempt fails (device slept mid-connect, interference), the work stays queued for the next wake; `attempts` increments. A **deadline timer** (options: `queue_timeout`, default **24 h** — safely above the 18 h max sleep interval) expires the slot: fire `opendisplay_content_expired` event, log a warning, clear the pending flag. This is the user's "backup timer / failure code".
- **Transport-agnostic (P4):** the manager's entry point is `async_device_seen(source: str)`. The BLE coordinator calls it with `"ble"`; a future WiFi presence tracker calls it with `"mdns"`. Nothing else changes.

#### D5 — Service-call semantics on a sleeping device

`upload_image` / `drawcustom` flow becomes:

1. Render + `prepare_image` immediately (unchanged — CPU work is front-loaded and reused on every retry).
2. **Freshness gate:** if sleep mode is active and the last advertisement is older than `sleep_timeout_ms + slack (~5 s)`, the device is provably asleep — **skip the doomed ~40 s bleak retry cycle** and queue directly. If the device was seen within the window (possibly still awake), attempt an immediate send as today.
3. On immediate-send success → done, exactly as today.
4. On `BLEConnectionError`/`BLETimeoutError` in sleep mode → queue into `pending_upload` instead of raising. Non-sleep devices keep today's strict failure.
5. The service returns response data (`SupportsResponse.OPTIONAL`): `{"status": "delivered" | "queued", "expires_at": …}` so automations can branch. It does **not** block until delivery (sleep intervals can be hours; blocking would time out the service call and jam automation queues).

This is precisely the tuning the user asked for regarding bleak dynamics: *when we do connect, it is triggered by the wake advertisement itself*, so the first attempt starts ~0.1–2 s into the 10 s window and the default 10 s/attempt budget is ample; and *we never burn 40 s of retries against a device we know is dark*. No py-opendisplay retry-parameter changes are required (though the delivery task will bound each wake attempt with an overall deadline of ~30 s so one bad wake can't overlap the next).

LED/buzzer services stay immediate-only (a notification that fires hours late is worse than an error); in sleep mode with the device dark they fail fast with a clear "device is sleeping" error.

**Revision 2026-07-07 — probe before queue.** Field review showed the freshness gate over-triggers: it treats `probably_asleep` as authoritative, but (a) BLE adverts are lossy — a busy scanner/proxy can miss an entire 10 s wake window, leaving `last_seen` stale while the tag is awake; (b) the Silabs firmware advertises continuously in EM2 and only enters true deep sleep (EM4) on explicit command, yet its power config (`power_mode==BATTERY`, `deep_sleep_time_seconds>0`) reads as sleepy, so such tags were queued while almost always reachable. Meanwhile HA retains a *connectable* `BLEDevice` for ~3–5 min after the last advert (habluetooth connectable-history pruning: scanner staleness ~195 s + 300 s sweep), so a connect attempt remains *possible* long after the gate flips at ~15 s.

Image sends now spend **one short connect attempt** (the "probe": `max_attempts=1`, `timeout=5 s` — `services.PROBE_CONNECT_TIMEOUT_S`) before queuing, instead of queuing blind. Cost analysis: a dark ESP32 has its radio fully off in timer deep sleep (`Firmware/src/main.cpp:365-383`), so a doomed probe costs the *device* zero battery and HA at most ~5 s — vs the old doomed 4×10 s ≈ 40 s budget this gate was built to avoid. A probe that lands cancels the wake-window timer and holds the tag awake (`main.cpp:138-170, 233-244`), converting into a normal full-quality delivery. A long-dark or never-seen tag has no connectable `BLEDevice`, so the probe short-circuits to the queue at near-zero cost. The probe is opt-out via the `probe_before_queue` option (default on). A post-probe freshness re-check closes the race where a wake advert arriving *during* the failed probe found no pending work and started no drain: if the tag looks fresh after queuing, `notify_device_seen("post-probe")` kicks an immediate drain. The ESP32 wake window is a fixed 10 s from wake (only a *connection* extends wakefulness), which is why the probe timeout sits deliberately under it. LED/buzzer and OTA gates are unchanged (still fail-fast).

#### D6 — Representing queued content: the image entity + a pending sensor

Per the user's instinct, the existing Display Content image entity is the queue's visible face:

- On **queue**: update the image entity immediately with the rendered frame (it now shows *intended* content) and set entity attribute `pending: true` plus `queued_at`. A new **binary sensor "Update pending"** exposes the same state for automations/dashboards (attributes: `queued_at`, `expires_at`, `attempts`).
- On **delivery**: clear `pending`; image already matches.
- On **expiry**: fire `opendisplay_content_expired`, set the binary sensor off, and revert the image entity attribute to `pending: false` with `last_error: expired` (the panel still shows the old frame; the image entity keeps the intended frame as the record of what was attempted — with the attribute making the mismatch explicit).

The queue itself is **memory-only in v1**: an HA restart drops a pending upload (documented limitation; the binary sensor goes off honestly). Persisting prepared frames (hundreds of KB) to a `Store` is a v2 option if real usage demands it — the delivery manager's API is designed so persistence can be added without touching callers.

#### D7 — Reboot-edge handling in sleep mode

Today's behavior — full entry reload on the advertised reboot edge — is wrong for sleepy devices twice over: the firmware's unpersisted `rebootFlag` makes every wake look like a reboot (churn loop, findings doc §Bug), and the reload's reconnect races the wake window. **Decision:** in sleep mode, the reboot edge sets `pending_config_resync` on the delivery manager instead of reloading. The resync rides the next delivery connection (or triggers one on the current wake, which is exactly when the edge is observed). Non-sleep devices keep the reload behavior. This makes the integration robust against the firmware bug while remaining correct once the firmware persists the flag.

#### D8 — OTA on sleepy devices

`update.py` connects directly today. In sleep mode: a user-initiated install registers `pending_ota` and reports progress state "waiting for device wake"; the next device-seen event starts the flash over the wake connection (device stays awake once connected — OTA duration is not window-constrained). Version *checks* remain opportunistic (piggyback on any delivery connection rather than connecting on a timer).

#### D9 — py-opendisplay: add the deep-sleep command (0x0052)

Not strictly required for ESP32 timer-driven sleep, but added for completeness and the Flex/Silabs story (where sleep is command-only): `OpenDisplayDevice.deep_sleep()` sending 0x0052, plus a `PowerConfig.deep_sleep_enabled` convenience property so the integration's D1 predicate lives next to the fields it reads. Also enables a future "sleep immediately after upload" optimization (save the remainder of a wake window after delivery).

### 2.3 Timing analysis — why the rendezvous works

With defaults (wake window W = 10 s; bleak timeout 10 s × 4 attempts):

| Step | Budget |
|---|---|
| Device wakes, first advertisement out | ~0.1–1 s into window (fast adv interval in `minimalSetup`) |
| Scanner → coordinator callback (local adapter) | < 0.5 s; ESPHome BLE proxy adds ~0.5–1.5 s |
| Delivery task start → `establish_connection` first attempt | < 0.1 s (already-running event loop task, `BLEDevice` fresh from this very advertisement) |
| Connection establishment | typically 1–3 s; up to 10 s budget fits inside remaining ~8 s window |
| After connect | device exits window logic and stays awake (`main.cpp:85-90`) — unlimited work time |

Failure modes are all recoverable: a missed window (proxy latency spike, connection slot exhaustion) simply waits `deep_sleep_time_seconds` for the next one; the deadline timer bounds total wait. Recommended user guidance (docs): keep `sleep_timeout_ms` ≥ 5000; typical `deep_sleep_time_seconds` 300–3600 gives content latency of at most one sleep interval.

### 2.4 Scenario walkthroughs (target behavior)

1. **HA restarts at 03:00; device sleeps 30 min.** Entry loads instantly from cache; all entities restored and available; `config_resync_pending` set. At the next wake (≤ 03:30) the delivery manager connects once, re-reads config, updates cache. No `ConfigEntryNotReady`, no flapping.
2. **Automation pushes a new image at 09:00; device sleeping until 09:25.** Image is rendered/prepared at 09:00; freshness gate sees stale adverts → queued instantly (no 40 s stall); service returns `queued`; image entity shows new frame with `pending: true`; binary sensor on. At 09:25 the wake advert triggers delivery; panel refreshes; `pending` clears; automations see `opendisplay_content_delivered`.
3. **Device battery dies / removed.** No wakes observed; after `missed_cycles × interval` entities go unavailable (honest signal); a queued upload expires after `queue_timeout` with `opendisplay_content_expired`.
4. **Five uploads queued while asleep.** Latest-wins: only the newest survives (existing semantics extended to the queue); each superseded call had returned `queued` and the final delivery event carries the last `queued_at`.
5. **Reboot flag seen on wake (firmware bug or real reboot).** No reload storm; config resync piggybacks the next delivery connection.
6. **New device adoption.** Unchanged — happens in the 2-minute first-boot window; first setup connects live and seeds the cache.

---

## 3. Component factoring

Proposed decomposition, smallest-surface-first. New modules in **bold**.

```
custom_components/opendisplay/
├── const.py            + option keys, defaults, event names, new dispatcher signals
├── config_flow.py      + OpenDisplayOptionsFlow (sleep_mode, missed_cycles, queue_timeout)
├── __init__.py         + config cache read/write; setup-from-cache branch;
│                         availability-interval registration; DeliveryManager wiring;
│                         runtime_data: delivery manager + sleep profile
├── coordinator.py      + async_subscribe_device_seen (mirror of async_subscribe_reboot)
├── **sleep.py**          SleepProfile: resolves options + device config → is_sleepy,
│                         availability_interval, freshness gate ("probably_asleep(now)")
├── **delivery.py**       DeliveryManager: pending slots (upload / config_resync / ota),
│                         device-seen handler, single-connection drain, deadline timers,
│                         delivered/expired events, state for sensors & diagnostics
├── services.py         + freshness gate + queue-on-failure via DeliveryManager;
│                         SupportsResponse.OPTIONAL with delivered/queued payload
├── image.py            + pending/queued_at attributes; shows queued frame at queue time
├── **binary_sensor.py**   "Update pending" entity backed by DeliveryManager state
├── update.py           + sleep-mode gating: pending_ota slot, "waiting for wake" state
├── diagnostics.py      + sleep profile + pending-slot state (redacted image bytes)
└── strings.json, translations/, icons.json — options flow + new entities + new errors

py-opendisplay/
├── protocol/commands.py   + DEEP_SLEEP = 0x0052 (+ encoder)
├── device.py              + async deep_sleep()
└── models/config.py       + PowerConfig.deep_sleep_enabled property

Firmware/  (recommended, decoupled — §5.4)
├── rebootFlag RTC persistence across deep sleep
└── post-reboot advertising grace window
```

Dependency direction: `services.py`, `update.py`, `image.py`, `binary_sensor.py` → `delivery.py` → `sleep.py` + `coordinator.py`. `delivery.py` owns all queue state; entities and diagnostics only read it; services only submit to it. The coordinator knows nothing about queues — it just announces "device seen".

---

## 4. Implementation plan

### Phase 0 — py-opendisplay groundwork *(small; independent release)*

1. `PowerConfig.deep_sleep_enabled` property (`models/config.py`): `power_mode == PowerMode.BATTERY and deep_sleep_time_seconds > 0`. Unit tests over parse/serialize round-trips.
2. `Command.DEEP_SLEEP = 0x0052` + `OpenDisplayDevice.deep_sleep()` (`device.py`, modeled on `reboot()` at `device.py:912`; fire-and-forget semantics — the ESP32 drops the link on entry, so tolerate a disconnect instead of awaiting an ACK; verify exact response behavior against `device_control.cpp:692` during implementation).
3. CLI: `opendisplay sleep <mac>` subcommand for testing.
4. Release (7.12.0) and bump `manifest.json` requirement in the integration.

*Acceptance: CLI can put an ESP32 dev board to sleep; config round-trip tests pass.*

### Phase 1 — Sleep awareness, availability, setup-from-cache *(the "entry survives" milestone)*

1. **`const.py`**: `CONF_SLEEP_MODE` (`auto|on|off`), `CONF_MISSED_CYCLES` (default 3), `CONF_QUEUE_TIMEOUT_HOURS` (default 24), event name constants, `CONF_CACHED_STATE` key.
2. **`sleep.py`**: `SleepProfile.from_entry(entry, config)` — resolves option override + `deep_sleep_enabled`; computes `availability_interval = interval × missed_cycles + wake_window_s + 60`; exposes `probably_asleep(last_seen)` using `sleep_timeout_ms + 5 s` slack. Pure functions, fully unit-testable.
3. **`__init__.py` — cache write**: after every successful interrogation, store `{"config": config_to_json(cfg), "firmware": fw, "is_flex": …, "cached_at": …}` under `entry.data[CONF_CACHED_STATE]` via `async_update_entry`.
4. **`__init__.py` — setup-from-cache branch**: if no connectable device *or* connect fails, and cached state exists with `deep_sleep_enabled` (or option forced on): rebuild `GlobalConfig` via `config_from_json`, construct runtime data without connecting, set `pending_config_resync` (consumed in Phase 2; in Phase 1 it may simply resync on next reboot-edge/reload), and proceed. Otherwise preserve today's `ConfigEntryNotReady`/`ConfigEntryAuthFailed` behavior exactly.
5. **`__init__.py` — availability interval**: when the profile is sleepy, call `async_set_fallback_availability_interval(hass, address, profile.availability_interval)` before starting the coordinator; re-apply on reload.
6. **`config_flow.py`**: add `OpenDisplayOptionsFlow` with the three options; options changes trigger entry reload (standard listener).
7. **strings/translations** for the options form.

*Acceptance: with a device configured for 10 min sleep — restart HA mid-sleep: entry loads, entities available and populated at next advert; no unavailable flap across three sleep cycles; options visible and effective after reload. Regression: non-sleepy device setup behavior unchanged (existing tests).*

### Phase 2 — Delivery manager and queued uploads *(the core feature)*

1. **`coordinator.py`**: `async_subscribe_device_seen(cb)` invoked from `_async_handle_bluetooth_event` after parsing (same pattern as `async_subscribe_reboot`).
2. **`delivery.py`**: `DeliveryManager(hass, entry, profile)`:
   - Slots as in D4; `submit_upload(prepared, params, preview_jpeg) -> PendingReceipt`; `request_config_resync()`; internal `_deliver()` task guarded by an asyncio flag + `ble_lock`, bounded by a ~30 s per-wake deadline; drain order upload → resync → ota.
   - Deadline timers via `async_call_later`; on expiry fire `opendisplay_content_expired` (payload: device_id, queued_at, attempts) and notify state listeners (dispatcher signal for entities).
   - On delivery: send existing `SIGNAL_IMAGE_UPDATED` (unchanged image pipeline), fire `opendisplay_content_delivered`, persist refreshed config to cache when a resync ran.
   - Teardown on unload: cancel timers + in-flight task (extend `async_unload_entry` alongside the existing upload-task cancel, `__init__.py:221-227`).
3. **`services.py`**: implement D5 — freshness gate; queue-on-connect-failure for `upload_image`/`drawcustom`; `SupportsResponse.OPTIONAL` returning `{"status", "expires_at"}`; LED/buzzer get the fast "device_sleeping" error when provably asleep. The existing latest-wins cancel logic remains for concurrent *live* uploads; the manager applies the same rule to the slot.
4. **`image.py`**: `pending`/`queued_at` attributes; listen to the manager's state signal; show queued frame at queue time (D6).
5. **`binary_sensor.py`**: "Update pending" backed by manager state; add platform to `_BASE_PLATFORMS`/`_FLEX_PLATFORMS`.
6. **`__init__.py`**: instantiate the manager in runtime data; setup-from-cache path now registers `pending_config_resync` with it, making the Phase 1 flag fully functional.
7. **strings/translations/icons** for the sensor, events, and the new error key.

*Acceptance: end-to-end on hardware — call `drawcustom` mid-sleep: returns `queued` in < 2 s, sensor on, image entity shows frame with `pending: true`; panel refreshes on next wake ≤ interval; sensor off; `opendisplay_content_delivered` observed. Kill the device (pull battery) with queued content: expiry event at deadline. Five rapid queued calls → one delivery of the last frame.*

### Phase 3 — Hardening and edges

1. **Reboot-edge rework (D7)**: in `__init__.py`, when profile is sleepy route the reboot subscription to `manager.request_config_resync()` instead of `_async_reload_after_reboot`.
2. **OTA gating (D8)** in `update.py`: `pending_ota` slot, `in_progress`/extra state "waiting for device wake", resume-on-wake.
3. **`diagnostics.py`**: sleep profile, availability interval, slot states (timestamps/attempts only — no image payloads).
4. **Encrypted devices**: queued delivery reuses the stored key; on `AuthenticationFailedError` during delivery → keep slot paused, trigger reauth flow (existing pattern in `_async_connect_and_run`), resume after successful reauth.
5. **Test suite**: unit tests for `sleep.py` and `delivery.py` (mock coordinator/device); integration-style tests with `inject_bluetooth_service_info` simulating wake cadences: setup-from-cache, flap-free availability, queue→deliver, queue→expire, reboot-edge resync, unload-with-pending.
6. **Docs**: user-facing page (docs/) covering options, latency expectations table (interval vs. worst-case content delay), and the memory-only queue limitation.

### Phase 4 — Firmware coordination *(separate repo; recommended, not blocking)*

1. Persist `rebootFlag` across deep sleep via `RTC_DATA_ATTR` mirror (findings doc, ~10 lines) — kills the churn loop at the source; D7 already defuses it HA-side.
2. Post-reboot advertising grace window (30–60 s after *any* boot where `woke_from_deep_sleep` is false) — makes commanded reboots recoverable.
3. Optional: `esp_sleep_enable_ext1_wakeup` on the button pin — user-initiated wake ("press button to sync now"), which composes perfectly with the delivery manager (button wake → advert → immediate delivery).

### Sequencing and risk

- Phases 0→1→2 are strictly ordered; 3 and 4 can proceed in parallel after 2.
- Riskiest assumption: wake-window connect reliability through ESPHome proxies (extra advert latency + connection-slot contention). Mitigation is inherent — a missed window costs one sleep interval, and Phase 2 acceptance is run against both a local adapter and a proxy.
- Backward compatibility: all changes are additive; non-sleepy devices follow existing code paths verbatim; new options default to today's behavior for them (`auto` resolves to off).
- HA quality-scale note: setup-from-cache plus passive-only polling keeps the integration aligned with bluetooth-integration guidance (no connections outside user intent or discovery).

---

## Appendix A — Options summary

| Option | Default | Meaning |
|---|---|---|
| `sleep_mode` | `auto` | `auto`: follow device power config; `on`/`off`: force |
| `missed_cycles` | 3 | Wake cycles missed before entities go unavailable |
| `queue_timeout` | 24 h | Pending content expires with `opendisplay_content_expired` |

## Appendix B — New events / signals / entities

| Surface | Name | Payload |
|---|---|---|
| Bus event | `opendisplay_content_delivered` | device_id, queued_at, attempts |
| Bus event | `opendisplay_content_expired` | device_id, queued_at, attempts |
| Service response | `upload_image` / `drawcustom` | `{status: delivered\|queued, expires_at}` |
| Entity | `binary_sensor.<name>_update_pending` | attrs: queued_at, expires_at, attempts |
| Entity attrs | image `display_content` | `pending`, `queued_at` |

## Appendix C — Open questions (decide during implementation)

1. Should a delivery connection also refresh the `last_seen`-adjacent sensors by reading live values (battery under load), or stay advert-only? (Lean: advert-only; keep connections short.)
2. `deep_sleep()` post-upload to return the device to sleep immediately after delivery (saves the rest of the wake window) — worth it once 0x0052 ships? (Lean: yes, guarded by an option, after measuring real window costs.)
3. v2 queue persistence across HA restarts via `helpers.storage.Store` — wait for user demand.
