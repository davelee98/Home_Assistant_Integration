# Should the BLE serialization lock live outside the device/entry context? (2026-07-16)

Working design memo (not user docs). Question: today the per-MAC BLE lock lives
on `runtime_data` and therefore only exists once an entry is set up. Should we
hoist it to a **domain-scoped, per-address lock** that exists before and
independent of any config entry?

**Verdict: yes — implement it, as a targeted hoist of the existing lock, not new
machinery.** It is load-bearing (the transport does not serialize per-MAC), it is
the only way to close the reauth-vs-live-op and onboarding overlap windows, and
it is cheap. But it is **defense-in-depth for the config-oversend crash, not the
fix** — ship the py-opendisplay client fixes (P1/P2 in
`CONFIG_OVERSEND_ROOT_CAUSE_SYNTHESIS_2026-07-16.md`) regardless; the lock reduces
how often the crash is provoked, it does not remove the root cause.

Source audited: HA `feat/play-melody @ d30f77a`, py-opendisplay
`feat/melody-notation @ 22f5319` (≡ pinned `v7.12.0`); transport layers
`bleak_retry_connector 4.6.0`, `habluetooth`, `aioesphomeapi` as installed.

---

## 1. Why a per-MAC lock is load-bearing (transport does NOT serialize)

An independent source audit of the connection-establishment path confirmed there
is **no per-MAC mutual exclusion anywhere** below the integration:

- `bleak_retry_connector.establish_connection` (4.6.0) holds no connect lock — the
  connect loop only wraps `client.connect()` in a timeout; grep for
  `Lock`/`Semaphore` in the connect path returns nothing.
- `habluetooth`'s `HaBleakClientWrapper.connect()` brackets the connect with a
  per-address *counter* (`_add_connecting`/`_finished_connecting`) that never
  rejects or awaits a second caller.
- `BleakSlotManager.allocate_slot` (`bluez.py:234-255`) is keyed by **adapter**
  and returns `True` immediately if the device path is already allocated
  (`:244-246`) — it caps *total* connections on the adapter, not concurrent
  connections to one MAC.
- The ESP32 `bluetooth_proxy` exposes only a zero-arg `can_connect()` capacity
  predicate and a per-proxy `(free, limit, allocated-slot-handles)` triple;
  `aioesphomeapi.bluetooth_device_connect` sends a second connect request for the
  same address with no in-flight dedup. A second connect to the same MAC is
  rejected only when the whole proxy is out of slots.

**Consequence:** two coroutines that open `OpenDisplayDevice` on the same MAC can
both reach the device and interleave GATT. The device has a single logical BLE
link and the library has no per-address lock, so this corrupts (confusing
`upload_error`, and — per the oversend synthesis — is one of the ways a second
config stream / stale frames get onto the link). The integration's existing
`runtime_data.ble_lock` is genuinely preventing this for steady-state ops; the
question is only about the paths it does **not** cover.

## 2. What the current lock covers, and the three gaps

`runtime_data.ble_lock` (`__init__.py:78`) is per-entry (one entry == one MAC) and
is acquired by every steady-state connect: delivery (`delivery.py:310`), services
draw/LED/buzzer (`services.py:436`), OTA (`update.py:248`), and drain-before
reload/unload (`__init__.py:385, 405`). The coordinator never connects (passive
adverts only), so it needs no lock.

It cannot cover connects that happen when `runtime_data` doesn't exist or isn't
referenced:

1. **Onboarding.** Config-flow probes (`config_flow.py:159`, plaintext probe →
   encrypted probe) and the setup-time connect (`__init__.py:238`) run before
   `runtime_data` is assigned (`__init__.py:305`). Encrypted onboarding thus opens
   2–3 unserialized connects within seconds.
2. **Reauth against a *live* entry — the strongest case, and not an onboarding
   issue.** `async_step_reauth_confirm` → `_async_test_connection`
   (`config_flow.py:328, 145`) opens a connection to the same MAC **without**
   taking `runtime_data.ble_lock`, while delivery / a service call / OTA may be
   holding it. Two live links to one device, fully unserialized. The current
   per-entry architecture *cannot* fix this: the config flow has no clean handle
   to the entry's runtime lock, and even reaching for it is a layering violation.
   A domain-scoped lock is the natural fix.
3. **Dual config flows.** `async_step_user` uses
   `async_set_unique_id(address, raise_on_progress=False)` (`config_flow.py:221`),
   so a manual-add flow can run concurrently with a discovery flow for the same
   MAC — two flows, two probes, one device.

Note there is currently **no `hass.data[DOMAIN]` bucket at all** — this is a pure
`runtime_data` integration. A pre-entry lock cannot live in `runtime_data` by
definition, so `hass.data[DOMAIN]` is the correct, HA-blessed home for it.

## 3. What the lock does and does not fix

**Fixes / improves:**
- Closes gap #2 (reauth vs. live op) — a real steady-state corruption window,
  impossible to fix within the per-entry model.
- Makes encrypted onboarding deterministic instead of racing (gap #1); with
  duplicate-frame transport (R1 in the synthesis), fewer overlapping streams means
  fewer chances to poison the queue.
- Serializes gap #3 dual-flow probes.
- Removes reliance on unload-drain ordering across reloads: the lock identity
  survives reload (runtime_data just references the domain object), so a
  concurrent OTA and a reload can't hold two different lock instances.

**Does NOT fix (must not be oversold):**
- The **root cause** of the oversend crash is transport-level duplicate
  notifications on a **single** connection plus client fragility. A serialization
  lock does nothing to a single connection that duplicates frames, and nothing to
  `read_firmware_version`'s echo-intolerant raw read. So even with a perfect lock,
  the crash still reproduces whenever the proxy duplicates a frame. **P1/P2
  (client) remain the actual fix; this lock is exposure reduction.**

## 4. Recommended design (small, low-risk)

Hoist, don't invent:

1. **Store the locks in the domain bucket**, keyed by normalized MAC:
   ```python
   def _ble_lock_for(hass: HomeAssistant, address: str) -> asyncio.Lock:
       locks = hass.data.setdefault(DOMAIN, {}).setdefault("ble_locks", {})
       return locks.setdefault(address.upper(), asyncio.Lock())
   ```
   (`address.upper()` matches the existing normalization at `__init__.py:221`,
   `services.py:381`.)
2. **Acquire it at the two currently-unlocked connect sites**, *inside* the
   existing `asyncio.timeout` so a stuck holder fails the probe/setup with the
   normal cannot_connect / ConfigEntryNotReady path instead of hanging:
   - `config_flow._async_test_connection` (covers both probes and the reauth
     probe).
   - `async_setup_entry`'s active connect (`__init__.py:237-247`).
3. **Reuse the same object as `runtime_data.ble_lock`**: in `async_setup_entry`,
   set `ble_lock=_ble_lock_for(hass, address)` when constructing
   `OpenDisplayRuntimeData` instead of `field(default_factory=asyncio.Lock)`. Every
   existing steady-state acquire site then shares identity with the flow/setup
   lock — no other call site changes.
4. **Flip `async_step_user` to `raise_on_progress=True`** to dedupe gap #3 at the
   flow layer (cheap complement).
5. Optionally skip the redundant setup-time interrogation by carrying probe #2's
   config into the created entry — halves encrypted-onboarding streams (separate
   change; reduces exposure further but not required for the lock).

**Deadlock audit (must preserve these orderings):**
- Reauth: the probe's `async with lock` exits *before*
  `async_update_reload_and_abort` runs, so the lock is released before the reload's
  unload drains it. ✅ (keep the acquire strictly around the connect, not around
  the whole step).
- Reload: `async_reload` = unload then setup, sequential in one task; unload drains
  (`async with lock: pass`) and returns before setup acquires. ✅
- `_async_reload_after_reboot` already drains-then-releases before calling
  `async_reload` specifically to avoid the unload deadlock (`__init__.py:381-387`);
  that pattern stays valid. ✅
- OTA already documents "only takes the lock here — never calls back into a locked
  op" (`update.py:241-243`); keep that no-reentrancy invariant.

**Lifecycle:** the dict leaks one small `asyncio.Lock` per MAC ever probed
(including devices probed but never added). This is negligible for a home
deployment; optionally delete the key in `async_remove_entry` on true removal. Do
not over-engineer with weakrefs — nothing holds a strong ref between ops, so a
`WeakValueDictionary` would drop the lock mid-life.

## 5. Alternatives considered

- **Keep per-entry lock; only reduce connection *count*** (raise_on_progress=True +
  carry probe config into the entry). Cheaper, cuts onboarding exposure, but does
  **not** fix gap #2 (reauth vs. live op). Complementary, not a substitute.
- **Do nothing at the lock layer; rely on client P1/P2.** Makes the crash
  non-fatal, but leaves real GATT interleaving (wasted connects, confusing errors,
  possibly corrupt reads) and the reauth race. Given the hoist is small, "both" is
  correct.

## 6. Priority

Implement it, but **after** the client fixes. In the synthesis ranking this is
**P6** — it reduces exposure and closes a real (reauth) race, but it does not
touch the root cause, so it must not be sequenced ahead of P1 (echo-tolerant
`read_firmware_version`) and P2 (interrogate hardening), which are what actually
stop the crash. Ship P1/P2 first; land this as the HA-side hardening, folding the
reauth-overlap fix (the part that specifically requires a lock *outside* the
device context) into the same change.
