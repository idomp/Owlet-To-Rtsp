"""
owlet_vitals.py — read Owlet Smart Sock vitals + Owlet Cam room sensors.

Reuses the existing OwletAPI Ayla auth (the same login the bridge already does
to fetch camera P2P creds). Owlet devices report to Ayla as "properties"; the
Smart Sock packs its live readings into a single JSON property (REAL_TIME_VITALS
on the newer socks, or individual named props on older ones), and the Owlet Cam
exposes room temperature / humidity / noise / brightness as named properties.

We DON'T hard-code your account's exact property names — Owlet renames them
between models/firmwares. Instead we:
  * parse any REAL_TIME_VITALS-style JSON blob with the known short keys, and
  * pattern-match the remaining named properties (TEMP/HUMID/NOISE/…).

`snapshot()` returns a clean normalized structure per device plus the raw
property map, so the /api/vitals/discover endpoint can show you exactly what
your devices expose.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

from owlet_api import redact_props

# --- Smart Sock 3 REAL_TIME_VITALS short keys -------------------------------
# Verified against ryanbdclark/pyowletapi const.py (VITALS_3). value ->
# (friendly_key, unit).
SOCK_KEYS: dict[str, tuple[str, str]] = {
    "ox":   ("oxygen", "%"),
    "hr":   ("heart_rate", "bpm"),
    "bat":  ("battery", "%"),
    "btt":  ("battery_minutes", "min"),
    "rsi":  ("signal_strength", "dBm"),
    "oxta": ("oxygen_avg", "%"),       # 10-minute average SpO2
    "bso":  ("base_station_on", ""),   # bool
    "sc":   ("sock_connection", ""),
    "st":   ("skin_temperature", "°C"),
    "ss":   ("sleep_state", ""),       # enum: 1 awake, 8 light, 15 deep
    "mv":   ("movement", ""),
    "mvb":  ("movement_bucket", ""),
    "aps":  ("alerts_paused", ""),
    "chg":  ("charging", ""),          # truthy while on the charger
    "alrt": ("alert_mask", ""),
    "ota":  ("update_status", ""),
    "srf":  ("readings_flag", ""),
    "sb":   ("brick_status", ""),
    "onm":  ("wellness_alert", ""),
    "mst":  ("monitoring_start", ""),
    "bsb":  ("base_battery", "%"),
    "hw":   ("hardware", ""),
}

# Smart Sock 2 (older) reports each reading as its own named Ayla property.
# Verified against pyowletapi VITALS_2. name -> friendly_key.
SOCK2_NAMED: dict[str, str] = {
    "OXYGEN_LEVEL": "oxygen",
    "HEART_RATE": "heart_rate",
    "BATT_LEVEL": "battery",
    "MOVEMENT": "movement",
    "CHARGE_STATUS": "charging",
    "BASE_STATION_ON": "base_station_on",
    "SOCK_CONNECTION": "sock_connection",
    "BLE_RSSI": "signal_strength",
    "OTA_STATUS": "update_status",
}

# sleep_state (ss) enum, from ryanbdclark/owlet const.py SLEEP_STATES.
SLEEP_STATES = {0: "unknown", 1: "awake", 8: "light sleep", 15: "deep sleep"}

# Readings that are meaningless while the sock is charging / off the foot.
# pyowletapi marks these unavailable when `charging` is truthy.
GATED_BY_CHARGE = ("oxygen", "heart_rate", "skin_temperature", "movement",
                   "oxygen_avg", "sleep_state")

# --- Owlet Cam room-sensor name patterns -> (friendly_key, unit) -------------
# Matched case-insensitively against the Ayla property name.
CAM_PATTERNS: list[tuple[re.Pattern, str, str]] = [
    (re.compile(r"humid", re.I),                         "humidity", "%"),
    (re.compile(r"(room|amb).*temp|temp.*(room|amb)|^temperature$|temp_c|tempc", re.I),
                                                          "temperature", "°C"),
    (re.compile(r"noise|decibel|sound_?level|\bdb\b", re.I), "noise", "dB"),
    (re.compile(r"bright|lux|light_?level|illum", re.I), "brightness", ""),
    (re.compile(r"\brssi\b|signal", re.I),               "rssi", "dBm"),
    (re.compile(r"motion", re.I),                        "motion", ""),
]

# Properties we never surface as a "sensor" (creds / control / noise).
SKIP = re.compile(r"uid|authkey|auth_key|password|token|secret|license|"
                  r"version|firmware|enable|setting|config|cmd|command", re.I)

VITALS_PROP = re.compile(r"vital|real_?time", re.I)


def _num(v: Any) -> Any:
    """Best-effort numeric coercion; leave strings/bools alone."""
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, (int, float)):
        return v
    if isinstance(v, str):
        s = v.strip()
        try:
            return int(s)
        except ValueError:
            try:
                return float(s)
            except ValueError:
                return v
    return v


def _parse_vitals_blob(value: Any) -> dict:
    """Parse a REAL_TIME_VITALS JSON value into friendly keys."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            return {}
    if not isinstance(value, dict):
        return {}
    out: dict[str, Any] = {}
    for k, v in value.items():
        friendly, _unit = SOCK_KEYS.get(k, (None, None))
        if friendly:
            out[friendly] = _num(v)
        else:
            out[f"raw_{k}"] = _num(v)
    _apply_charge_gate(out)
    return out


def _apply_charge_gate(s: dict) -> None:
    """While charging / off the foot, vitals are invalid -> null them out.

    This is pyowletapi's canonical validity rule (not a -1 sentinel guess)."""
    charging = s.get("charging")
    on_charger = bool(charging) and str(charging) != "0"
    for k in GATED_BY_CHARGE:
        if k in s:
            v = s[k]
            # also treat a literal 0/negative HR or O2 as "no reading"
            bad = on_charger or (k in ("oxygen", "heart_rate")
                                 and isinstance(v, (int, float)) and v <= 0)
            if bad:
                s[k] = None


def _props_to_map(props: Any) -> dict[str, Any]:
    """Ayla /properties.json -> {name: value}."""
    out: dict[str, Any] = {}
    if isinstance(props, list):
        for p in props:
            pr = p.get("property", p) if isinstance(p, dict) else {}
            name = pr.get("name")
            if name is not None:
                out[name] = pr.get("value")
    return out


def extract_sensors(prop_map: dict[str, Any]) -> dict[str, Any]:
    """Normalize one device's property map into friendly sensor readings."""
    sensors: dict[str, Any] = {}
    for name, value in prop_map.items():
        # Sock 3: the packed REAL_TIME_VITALS JSON blob
        if VITALS_PROP.search(name) and isinstance(value, (str, dict)):
            blob = _parse_vitals_blob(value)
            if blob:
                sensors.update(blob)
                continue
        # Sock 2: individually named vitals properties
        if name in SOCK2_NAMED:
            sensors[SOCK2_NAMED[name]] = _num(value)
            continue
        if SKIP.search(name):
            continue
        # Speculative cam room-sensor matching (cam sensors aren't actually on
        # the cloud API per ha-owlet — kept as a discovery aid only).
        for pat, friendly, _unit in CAM_PATTERNS:
            if pat.search(name):
                n = _num(value)
                if isinstance(n, (int, float)) or (isinstance(n, str) and n):
                    sensors.setdefault(friendly, n)
                break
    # Sock 2 has no packed blob, so gate its named vitals here too.
    _apply_charge_gate(sensors)
    return sensors


def unit_for(friendly: str) -> str:
    for _k, (f, u) in SOCK_KEYS.items():
        if f == friendly:
            return u
    for _pat, f, u in CAM_PATTERNS:
        if f == friendly:
            return u
    return ""


class OwletVitals:
    """Thin reader on top of an authenticated OwletAPI instance."""

    def __init__(self, api, log=None):
        self.api = api
        self.log = log or (lambda m: None)

    def _properties(self, dsn: str) -> dict[str, Any]:
        props = self.api._ayla_get(f"/dsns/{dsn}/properties.json")  # noqa: SLF001
        return _props_to_map(props)

    @staticmethod
    def _classify(dsn: str, model: str, name: str) -> str:
        if str(dsn).upper().startswith("OCD"):
            return "cam"
        if re.search(r"sock|sm0|dream|monitor|owlet", str(model) + str(name), re.I):
            return "sock"
        return "device"

    def snapshot(self) -> list[dict]:
        """Login (if needed) + read every device's sensors.

        Returns a list of {dsn, name, model, kind, sensors, raw_props}.
        Socks get an APP_ACTIVE poke first so their vitals are live, not stale.
        """
        if not self.api.access_token:
            self.api.authenticate()
        devs = []
        for d in self.api.devices():
            dsn = d.get("dsn")
            if not dsn:
                continue
            model = d.get("oem_model") or d.get("model") or ""
            name = d.get("product_name") or d.get("device_type") or dsn
            devs.append((dsn, model, name, self._classify(dsn, model, name)))

        # Wake every sock, then give the cloud a moment to publish fresh vitals.
        woke = False
        for dsn, _m, _n, kind in devs:
            if kind == "sock":
                if self.api.activate(dsn):
                    self.log(f"[vitals] APP_ACTIVE -> {dsn} (waking sock)")
                    woke = True
        if woke:
            time.sleep(2.5)

        out: list[dict] = []
        for dsn, model, name, kind in devs:
            try:
                pmap = self._properties(dsn)
            except Exception as e:  # noqa: BLE001
                self.log(f"[vitals] properties({dsn}) error: {e}")
                pmap = {}
            out.append({
                "dsn": dsn,
                "name": name,
                "model": model,
                "kind": kind,
                "sensors": extract_sensors(pmap),
                # secret-looking property values masked: this map is logged by
                # the discovery button and stored in snapshot.json
                "raw_props": redact_props({k: pmap[k] for k in sorted(pmap)}),
            })
        return out
