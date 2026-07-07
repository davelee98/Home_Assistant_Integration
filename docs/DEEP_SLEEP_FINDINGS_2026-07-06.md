# OpenDisplay Deep Sleep — Cross-Stack Findings

*Investigation of deep sleep behavior across Firmware (ESP32/nRF), Firmware_Silabs, py-opendisplay, and the Home Assistant integration — 2026-07-06*

## TL;DR

- **The ESP32 firmware's deep-sleep loop itself is sound** (timer wake, ~10 s advertising window, first-boot grace period, panel power-down).
- **Bug: `rebootFlag` is not persisted across deep sleep**, so every wake advertises "I rebooted". Combined with the HA integration's reload-on-reboot behavior, this can produce an intermittent **reload → connect → sleep → wake → reload churn loop** that drains battery and spams HA.
- **Gap: HA has no wake-window awareness.** Service calls connect immediately and fail if the device is mid-sleep; nothing waits for the next wake advertisement. Uploads to a sleeping device succeed only by luck.
- **Gap: a commanded reboot leaves almost no reconnect window** — the device re-enters deep sleep within ~1 s because the first-boot grace period is skipped.
- **Gap: py-opendisplay does not implement the deep-sleep command (0x0052)**, so neither HA nor the CLI can put a device to sleep, even though ESP32 and Silabs firmware both handle it.

## How deep sleep works today

### ESP32 (reference firmware)

Deep sleep engages when `power_option.power_mode == 1` (BATTERY) **and**
`deep_sleep_time_seconds > 0`.

- **Entry** — `enterDeepSleep()` (`Firmware/src/main.cpp:275`): stops advertising, deinits BLE, arms **timer wakeup only** (`esp_sleep_enable_timer_wakeup`, `main.cpp:301`), holds the power latch, calls `esp_deep_sleep_start()`. The loop enters it whenever `bleActive` is false (no connection, empty command/response queues, no EPD refresh, no WiFi LAN session).
- **Wake** — deep-sleep wake is a full CPU boot. `setup()` detects the wake cause and runs `minimalSetup()` (`main.cpp:245`): config init, IO init, BLE advertising only — no display init, no WiFi, no boot screen.
- **Wake window** — the device advertises for `sleep_timeout_ms` (default **10 s** when 0, `main.cpp:94-96`). If nothing connects, it returns to sleep. If a central connects, `fullSetupAfterConnection()` (`main.cpp:256`) brings up WiFi and the panel driver state, and queued BLE commands are processed on subsequent loop passes.
- **First boot** — a 2-minute grace period before the first deep sleep (`FIRST_BOOT_DEEP_SLEEP_DELAY_MS`, `main.cpp:180-196`) gives the user time to adopt/configure. Applies only when `deep_sleep_count == 0`.
- **State across sleep** — `woke_from_deep_sleep`, `deep_sleep_count`, `displayed_etag` are `RTC_DATA_ATTR` (`Firmware/src/main.h:285-294`) and survive. Ordinary globals (including `rebootFlag`) reset every wake.

### nRF

No deep sleep. `sleep_timeout_ms` is used as an idle-loop delay; the SoftDevice's
System-ON idle is the low-power state. The 0x0052 command replies
"not supported" (`Firmware/src/device_control.cpp:703`).

### Silabs (Flex)

Deep sleep is **command-driven only**: 0x0052 sets `s_pending_deep_sleep`; after the
BLE connection closes, the firmware powers off the panel, arms **EM4 wake on button
and NFC field-detect**, and calls `EMU_EnterEM4()`
(`Firmware_Silabs/opendisplay_ble.c:1921-1926`). There is **no timer wake** —
`deep_sleep_time_seconds` exists in the Silabs config struct but is unused. A
sleeping Flex device stays down until a button press or NFC field.

### py-opendisplay

Serializes/parses the power config fields (`power_mode`, `sleep_timeout_ms`,
`deep_sleep_time_seconds`, `deep_sleep_current_ua`) but **exposes no deep-sleep
command** — only `reboot()` (0x000F). Connections go through bleak-retry-connector
(4 attempts × 10 s timeout, plus one GATT-cache-clear retry,
`py-opendisplay/src/opendisplay/transport/connection.py`).

### Home Assistant integration

- Monitoring is fully passive: `OpenDisplayCoordinator` is a
  `PassiveBluetoothDataUpdateCoordinator` fed by advertisements only.
- Connections happen for: entry setup (firmware/config interrogation,
  `__init__.py:104`), service calls (`services.py:280` `_async_connect_and_run`),
  and OTA (`update.py`).
- The coordinator watches the advertised **reboot flag** (bit 1 of the status
  byte) and, on a False → True edge, **reloads the config entry** — which opens a
  new BLE connection to re-read firmware + config
  (`coordinator.py:129-150`, `__init__.py:180-195`).

## Bug: deep-sleep wake is indistinguishable from a reboot

`rebootFlag` is a plain global initialized to 1 (`Firmware/src/main.h:126`),
unlike its RTC-persisted neighbors. Since deep-sleep wake is a full boot, **every
wake re-advertises `reboot_flag = 1`**.

The churn loop:

1. HA connects (setup, service call, or a previous iteration of this loop) →
   firmware clears the flag on connect (`Firmware/src/esp32_ble_callbacks.h:42`).
2. On disconnect, advertising restarts with flag = 0
   (`esp32_restart_ble_advertising` → `updatemsdata`,
   `Firmware/src/ble_init.cpp:165`), but only for a sub-second burst before the
   loop re-enters deep sleep.
3. If HA's scanner catches one of those flag-0 adverts, `_last_reboot_flag`
   becomes False.
4. Next wake advertises flag = 1 → False → True edge → HA reloads the entry →
   connects → interrogates → clears flag → device sleeps → **go to 2**.

Each cycle also runs `initWiFi()` on the device (`fullSetupAfterConnection()`),
compounding the battery cost. Because step 3 depends on catching a brief
advertising burst, the loop is **intermittent** — it presents as unexplained
battery drain and periodic "Device rebooted since last connection" log lines at
the deep-sleep cadence.

The coordinator's edge detection is otherwise well designed: None → True is
ignored (setup already synced) and a flag stuck at True self-guards. The defect
is purely that the firmware loses the flag across sleep.

**Suggested fix (firmware, ~10 lines):** mirror `rebootFlag` into an
`RTC_DATA_ATTR` variable in `enterDeepSleep()` and restore it in `setup()` on a
deep-sleep wake. Power-on reset clears RTC RAM, so genuine cold boots still
advertise 1. The reboot command path (`esp_restart()` preserves RTC RAM) must
explicitly set the mirror back to 1 before restarting.

## Gaps

### 1. HA cannot reliably reach a sleeping device

`_async_connect_and_run` (`services.py:289`) resolves the cached `BLEDevice` and
connects immediately. Against a device sleeping e.g. 5 minutes with a 10 s wake
window, an arbitrary service call has a few-percent success rate; failures raise
`upload_error` and the image is dropped (no queue, no retry-at-next-wake).
The integration already has everything needed to do better: the passive
coordinator sees every wake advertisement, and `entry.runtime_data.device_config`
contains the power settings.

**Suggested fix (integration):** a "wait for next advertisement, then connect"
helper used by services, setup retry, and OTA when the device config indicates
battery + deep sleep. This is the standard ESL/e-ink hub pattern (queue content,
deliver at check-in).

### 2. Commanded reboot → near-instant sleep

`deep_sleep_count` survives `esp_restart()` (RTC RAM persists across software
resets), so after a reboot command the first-boot condition
(`main.cpp:180`, requires `deep_sleep_count == 0`) is false and the device
re-enters deep sleep within about a second. HA's reboot-triggered reload then
usually misses, lands in `ConfigEntryNotReady`, and the entities sit unavailable
until a timer-backoff retry happens to coincide with a wake window.

**Suggested fix (firmware):** grant a short (e.g. 30–60 s) advertising grace
period after any normal boot, not just the very first one.

### 3. Availability flapping on long sleep intervals

Entity availability tracks advertisement freshness. If
`deep_sleep_time_seconds` exceeds HA Bluetooth's stale-advertisement timeout
(on the order of a few minutes), every sleep cycle marks all entities
unavailable and the next wake marks them available again — even though the
device is healthy. Setup retries are timer-based only; nothing retries
immediately when the device reappears.

### 4. py-opendisplay lacks the deep-sleep command

Firmware handles 0x0052 on ESP32 (`Firmware/src/device_control.cpp:691`) and
Silabs, but the library exposes only `reboot()`. HA therefore has no way to
command a device to sleep (relevant for Flex devices, where sleep is
command-driven).

## Design notes / quirks

- `sleep_timeout_ms` has two meanings: post-wake **advertising window** on ESP32
  (`main.cpp:94`), idle-loop delay on nRF. Default window is 10 s when the field
  is 0.
- `connectionRequested` (bit 2 of the advertised status byte,
  `Firmware/src/main.h:127`) is reserved and unused — a natural hook for a
  future "content pending / stay awake" handshake between HA and the device.
- Wake-on-BLE from true deep sleep is **not possible on ESP32** — the radio is
  off; wake sources are timer/GPIO/touch/ULP only. The near-equivalent (light
  sleep + BT modem sleep, always connectable) costs ~0.5–2 mA average vs
  ~10–20 µA in deep sleep. nRF52/EFR32 keep the radio scheduler alive at µA
  levels in their idle states, which is why those targets don't need this
  machinery. A cheap firmware improvement: also arm `esp_sleep_enable_ext1_wakeup`
  on the button pin so a user can wake the tag on demand (the Silabs target
  already wakes from EM4 on button/NFC).

## What was verified to work

- Wake window logic is correctly guarded; queued commands received during the
  wake window are processed after `fullSetupAfterConnection()`.
- Panel power is managed per-refresh (`pwrmgm(false)` after refresh), so the
  display rail is already off at sleep entry; external flash is parked
  (CS high, CLK/MOSI low).
- First-boot 2-minute adoption grace period works (power-on resets clear
  `deep_sleep_count`).
- The HA coordinator's reboot edge detection ignores the initial observation and
  a permanently-set flag; the reload defers until any in-progress upload
  finishes (`__init__.py:190-195`).
- Upload concurrency is last-writer-wins: a new upload cancels the in-flight one
  (`services.py:396-401`) — appropriate for a display where only the latest
  image matters.
