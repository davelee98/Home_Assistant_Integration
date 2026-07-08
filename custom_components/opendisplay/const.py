"""Constants for the OpenDisplay integration."""

DOMAIN = "opendisplay"
CONF_ENCRYPTION_KEY = "encryption_key"

# Dispatcher signals (suffixed with the device address at send/subscribe time).
# Carries the JPEG preview of the frame shown by the image entity.
SIGNAL_IMAGE_UPDATED = f"{DOMAIN}_image_updated"
# Carries a DeliverySnapshot describing the delivery manager's pending state
# (see delivery.py). Consumed by the image entity and the "update pending"
# binary sensor.
SIGNAL_PENDING_STATE = f"{DOMAIN}_pending_state"

# --- Options (options flow) -------------------------------------------------
# Deep-sleep handling mode: follow the device power config, or force on/off.
CONF_SLEEP_MODE = "sleep_mode"
SLEEP_MODE_AUTO = "auto"
SLEEP_MODE_ON = "on"
SLEEP_MODE_OFF = "off"
DEFAULT_SLEEP_MODE = SLEEP_MODE_AUTO

# Consecutive expected wakes that may be missed before entities go unavailable.
CONF_MISSED_CYCLES = "missed_cycles"
DEFAULT_MISSED_CYCLES = 3

# Hours a queued upload survives before expiring (safely above the 18 h max
# firmware sleep interval).
CONF_QUEUE_TIMEOUT_HOURS = "queue_timeout_hours"
DEFAULT_QUEUE_TIMEOUT_HOURS = 24

# Probe a probably-asleep device with one short connect attempt before queuing
# an image send. Catches wake adverts the scanner missed and Silabs tags that
# advertise continuously despite a battery+deep-sleep power config. Bounded
# cost: one attempt with a short timeout (see services.PROBE_CONNECT_TIMEOUT_S).
CONF_PROBE_BEFORE_QUEUE = "probe_before_queue"
DEFAULT_PROBE_BEFORE_QUEUE = True

# --- Cache ------------------------------------------------------------------
# entry.data key holding the serialized device state used to set up the entry
# without connecting when a sleepy device is dark at startup.
CONF_CACHED_STATE = "cached_state"

# --- Bus events -------------------------------------------------------------
EVENT_CONTENT_DELIVERED = f"{DOMAIN}_content_delivered"
EVENT_CONTENT_EXPIRED = f"{DOMAIN}_content_expired"
