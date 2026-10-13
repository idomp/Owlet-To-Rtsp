# MQTT / Home Assistant — what the bridge publishes

`vitals_poller.py` publishes the Owlet Cam room sensors (and, only if you opt
in, Smart Sock vitals) to an MQTT broker using Home Assistant MQTT discovery,
so HA creates the entities by itself.

## Turning it on and off

| Setting | Effect |
|---|---|
| `OWLET_MQTT_HOST` (+ `_PORT`, `_USER`, `_PASS`, `_PREFIX`) | Broker connection. The same fields are in the control panel; UI values win over env vars. `_PREFIX` is the HA discovery prefix (default `homeassistant`). |
| `OWLET_SENSOR_PUBLISH=0` | Disables the publisher process entirely: no MQTT, no `snapshot.json`, no HUD overlay text. Default `1`. |
| `OWLET_VITALS_POLL=1` | Also polls Smart Sock vitals from Owlet's cloud. **Off by default**: every cycle it writes `APP_ACTIVE=1` to the sock through the cloud. |
| `OWLET_VITALS_INTERVAL` | Seconds between publish cycles (default 15). |
| `OWLET_SENSOR_STALE` | Seconds after which a camera reading is no longer current (default 60). |

## Topics

`<node>` is the camera name for a camera and the device serial for a sock, both
lower-cased with every non-alphanumeric character turned into `_`.

| Topic | Retained | Payload |
|---|---|---|
| `owlet/bridge/status` | yes | `online` on every (re)connect; `offline` is the broker's last will when the bridge goes away. |
| `owlet/<node>/availability` | yes | `online` while the device has at least one fresh reading, else `offline`. |
| `owlet/<node>/state` | yes | JSON: one key per sensor (temperatures in °F) plus `measured_at` (UTC, ISO 8601) — the time of the newest reading, not the publish time. |
| `<prefix>/sensor/owlet_<node>_<key>/config`, `<prefix>/binary_sensor/owlet_<node>_<key>/config` | yes | HA discovery config for one entity. |

Camera keys: `temperature`, `humidity`, `noise`, `brightness`, `wifi_rssi`
(sensors), `motion`, `sound` (binary sensors). Sock keys: `heart_rate`,
`oxygen`, `oxygen_avg`, `skin_temperature`, `battery`, `battery_minutes`,
`signal_strength`, `sleep_state`, `movement`, `base_station_on`, `charging`.

## Availability model

Each entity lists two availability topics with `availability_mode: all`, so it
is available only while **both** are `online`:

1. `owlet/bridge/status` — is the bridge connected at all;
2. `owlet/<node>/availability` — does this device have any fresh reading.

On every successful connect, including paho's silent automatic reconnects, the
bridge republishes `online`, every discovery config sent so far and every
device's availability, and re-sends each state on the next cycle. The replay
is published under the same lock the publisher uses to update its availability
cache, so a device that goes `offline` while a reconnect is being replayed can
never be left showing the old `online`.

## Freshness and expiry, per field

`tutk_client` stamps each camera field with its own measurement time
(`field_ts` in `<config>/vitals/cam-<name>.json`). A field older than
`OWLET_SENSOR_STALE` is dropped before publishing:

* a field HA already knows that is no longer fresh is sent as `null` in the
  state JSON (HA shows "unknown", never the old number);
* a device with no fresh field at all is marked `offline` and gets no state;
* an unchanged payload (same values, same `measured_at`) is not re-sent.

The same rule applies to the control panel's `/api/vitals` and the video HUD:
an expired reading is absent there, not shown as live.

## Discovery record and retirement

Every discovery topic the bridge publishes is recorded in
`<config>/vitals/discovery.json` (written atomically), together with the
source that produced it (`cam` for the local camera sensors, `cloud` for the
sock poller) and the device's state and availability topics.

On every broker connect and every poll cycle, each recorded entity whose source
is no longer running — typically every sock entity once `OWLET_VITALS_POLL` is
back at `0` — is retired:

1. an **empty retained payload** on its discovery topic (this is how Home
   Assistant removes an MQTT-discovered entity, and it also clears the
   retained config from the broker);
2. its retained state topic is cleared the same way;
3. `offline` on its availability topic;
4. its record entry is marked `retired_at: <unix time>` (it is not deleted
   yet).

The three retirement messages are published with QoS 1 and retained, without
waiting for the broker (they are also sent from the MQTT network callback,
which must not block). An accepted publish is not yet proof of delivery — a
disconnect or a crash can lose it before it leaves the socket — so the bridge
keeps the delivery handle the MQTT client returns for each of these messages
(paho's `MQTTMessageInfo`) and waits for the broker's acknowledgement (the QoS 1
`PUBACK`, which marks the handle published). The handles are checked at the
start of each retirement pass (every poll cycle while a retirement is pending,
and every connect). Only retirement messages are tracked this way, at most the
last few sends per message of an entry that is retired and not yet confirmed;
the ordinary state, discovery and availability messages leave nothing behind.

* once **all three** messages of an entry are acknowledged, the entry is
  confirmed and dropped from the record on the next write; it is never sent
  again;
* an entry that is marked but not yet confirmed is sent again on every broker
  connect and, between connects, **at most once every 10 minutes** — not on
  every poll cycle;
* an entry still unconfirmed **7 days** after it was marked is pruned from the
  record with a log line
  `WARNING: the broker never acknowledged the retirement of MQTT discovery topic <topic> …`.
  The bridge stops trying at that point; if Home Assistant still shows the
  entity, delete it by hand (see below).

A state or availability topic that an active entity still uses is never
cleared or set `offline` by this. Acknowledgements are tracked in memory only:
after a restart, an entry still marked in the record is sent again on the
first connect and confirmed by that send's acknowledgements.

Sending needs a live broker connection (pruning does not). If any of the three
publishes is refused by the client, the entry stays unmarked and is retried on
the next poll cycle or connect. If the source is switched back on, the next
discovery publish for that entity replaces the marked entry with a normal one.

Without this, the sock's retained discovery configs and last readings stayed
in the broker after the poller was switched off, and because the bridge-wide
`owlet/bridge/status` goes `online` again for the camera entities, Home
Assistant showed the old sock readings as available indefinitely.

## Upgrading from a version without the record

Versions before the discovery record kept no list of what they had published,
so entities they created for a source that is now off (usually the sock) are
not known to the bridge and are not retired automatically. Clean them up once,
either way:

* in Home Assistant, **Settings → Devices & services → MQTT → Devices**, open
  the stale Owlet sock device and delete it; or
* run the bridge once with `OWLET_VITALS_POLL=1` until the sock entities have
  been published again (that records them; it needs the sock to be on and
  reporting, and for that run it writes `APP_ACTIVE` to the sock as described
  above), then set it back to `0` and restart: the bridge retires them itself.

If the broker still holds retained sock topics after that, clear them with any
MQTT client, for example
`mosquitto_pub -h <broker> -t '<topic>' -r -n` for each `owlet/<node>/…`
topic of the sock.
