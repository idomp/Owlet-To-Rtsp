"""
owlet_api.py — real Owlet cloud login + device discovery + diagnostics.

Flow (reverse-engineered, same as pyowletapi / owlet_monitor):
  1. Firebase verifyPassword(email, password)            -> Firebase idToken (JWT)
  2. GET https://ayla-sso.owletdata.com/mini/  (Bearer JWT) -> mini_token
  3. POST {ayla}/users/sign_in or token_sign_in (app_id/app_secret + mini_token)
                                                          -> Ayla access_token
  4. GET {ads}/apiv1/devices.json  (auth_token)          -> device list

The Owlet *Sock* lives on Ayla. The *Cam* uses Kalay/TUTK — its UID/AuthKey may
or may not appear in the Ayla device list / properties. `diagnose()` dumps
EVERYTHING the account returns so we can locate the camera credentials from the
logs you share. Passwords/tokens are masked in the log.
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable

import requests

REGIONS: dict[str, dict[str, str]] = {
    "world": {
        "firebase_key": "AIzaSyCsDZ8kWxQuLJAMVnmEhEkayH1TSxKXfGA",
        "mini": "https://ayla-sso.owletdata.com/mini/",
        "sign_in": "https://user-field-1a2039d9.aylanetworks.com/api/v1/token_sign_in",
        "base": "https://ads-field-1a2039d9.aylanetworks.com/apiv1",
        "app_id": "sso-prod-3g-id",
        "app_secret": "sso-prod-UEjtnPCtFfjdwIwxqnC0OipxRFU",
    },
    "europe": {
        "firebase_key": "AIzaSyDm6EhV70wudwN3iOSq3vTjtsdGjdFLuuM",
        # NB: EU uses the .eu. mini host (world host returns 401 for EU users).
        "mini": "https://ayla-sso.eu.owletdata.com/mini/",
        "sign_in": "https://user-field-eu-1a2039d9.aylanetworks.com/api/v1/token_sign_in",
        "base": "https://ads-field-eu-1a2039d9.aylanetworks.com/apiv1",
        "app_id": "OwletCare-Android-EU-fw-id",
        "app_secret": "OwletCare-Android-EU-JKupMPBoj_Npce_9a95Pc8Qo0Mw",
    },
}

FIREBASE_URL = ("https://www.googleapis.com/identitytoolkit/v3/relyingparty/"
                "verifyPassword?key={key}")
# Camera Key-Management Service: returns the Kalay tutkid/UID + AuthKey + AV
# password for a camera DSN, authorized with the raw Firebase idToken.
KMS_URL = "https://camera-kms.owletdata.com/kms/{dsn}"

# Owlet's Firebase API key is restricted to the official Android app, so Google
# requires the app's identity headers or it returns 403 "Requests from this
# Android client application <empty> are blocked".
ANDROID_PACKAGE = "com.owletcare.owletcare"
ANDROID_CERT = "2A3BC26DB0B8B0792DBE28E6FFDC2598F9B12B74"

# Fields that may carry the Kalay camera credentials.
CRED_KEYS = re.compile(
    r"uid|authkey|auth_key|p2p|kalay|tutk|dsn|serial|model|oem|product|"
    r"init|enc|secret|license|dtls|psk|camera|cam_", re.I)
UID_RE = re.compile(r"\b[A-Z0-9]{16,20}\b")

LogFn = Callable[[str], None]


def _mask(s: str) -> str:
    """Never reveal any character of a secret (token, AuthKey, password) —
    the old first-4/last-3 form leaked partial tokens into logs."""
    if not s:
        return s
    return f"<redacted {len(s)} chars>"


def mask_id(s: str) -> str:
    """Identifiers (camera UID, email): enough to tell two apart, not to reuse."""
    s = str(s or "")
    if "@" in s:
        user, _, dom = s.partition("@")
        return (user[:1] + "***@" + dom) if user else "***"
    return s[:4] + "…" + s[-2:] if len(s) > 8 else "***"


SECRET_NAME_RE = re.compile(r"secret|token|pass|psk|auth|key|cred", re.I)


def redact_props(props: dict) -> dict:
    """Copy of a property map with secret-looking values masked (for logs and
    the vitals snapshot file)."""
    return {k: (_mask(str(v)) if SECRET_NAME_RE.search(str(k)) and v not in (None, "") else v)
            for k, v in (props or {}).items()}


class OwletError(Exception):
    pass


class OwletAPI:
    def __init__(self, region: str, email: str, password: str, log: LogFn | None = None):
        if region not in REGIONS:
            raise OwletError(f"region must be one of {list(REGIONS)}")
        self.cfg = REGIONS[region]
        self.region = region
        self.email = email
        self.password = password
        self.log = log or (lambda m: None)
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": "OwletCare/Android"})
        self.access_token: str | None = None
        self.firebase_token: str | None = None

    # -- step 1: Firebase ---------------------------------------------------
    def _firebase(self) -> str:
        self.log(f"[1/4] Firebase verifyPassword as {mask_id(self.email)} ({self.region}) …")
        r = self.s.post(
            FIREBASE_URL.format(key=self.cfg["firebase_key"]),
            json={"email": self.email, "password": self.password,
                  "returnSecureToken": True},
            headers={"X-Android-Package": ANDROID_PACKAGE,
                     "X-Android-Cert": ANDROID_CERT},
            timeout=20,
        )
        if r.status_code != 200:
            self.log(f"      Firebase HTTP {r.status_code}: {r.text[:300]}")
            raise OwletError(f"Firebase login failed ({r.status_code})")
        jwt = r.json().get("idToken")
        if not jwt:
            raise OwletError("Firebase returned no idToken")
        self.firebase_token = jwt
        self.log(f"      OK idToken={_mask(jwt)}")
        return jwt

    # -- camera credentials: KMS (needs only the Firebase idToken) ---------
    def camera_credentials(self, dsn: str) -> dict:
        """GET the Kalay UID + AuthKey + AV password for a camera DSN.

        Returns {"uid", "authkey", "av_password"}. Only needs the Firebase
        idToken (so a plain login is enough — no Ayla sign-in required).
        """
        if not self.firebase_token:
            self._firebase()
        # Normalize: Owlet DSNs are uppercase, and the camera DSN starts with the
        # letter O (OCD…). A leading zero ("0CD…") is a common O/0 typo, so fix it.
        dsn = (dsn or "").strip().upper()
        if dsn.startswith("0CD"):
            fixed = "O" + dsn[1:]
            self.log(f"[kms] DSN '{dsn}' starts with a zero — Owlet cam DSNs start "
                     f"with the letter O; using '{fixed}'")
            dsn = fixed
        url = KMS_URL.format(dsn=dsn)
        self.log(f"[kms] GET camera key for {dsn} …")
        r = self.s.get(url, headers={"Authorization": self.firebase_token}, timeout=20)
        if r.status_code != 200:
            self.log(f"      KMS HTTP {r.status_code}: {r.text[:200]}")
            if r.status_code == 403:
                self.log("      → 403 = this DSN isn't a camera on your account. "
                         "Double-check it: letter O vs zero, and no typos (it looks "
                         "like OCD followed by digits).")
            raise OwletError(f"camera KMS failed ({r.status_code})")
        d = r.json()
        uid = d.get("tutkid") or d.get("uid")
        authkey = d.get("authKey") or d.get("authkey")
        pw = d.get("password")
        if not uid:
            raise OwletError(f"KMS response had no tutkid: {list(d)}")
        self.log(f"      OK uid={mask_id(uid)} authKey={_mask(authkey or '')} "
                 f"password={_mask(pw or '')}")
        return {"uid": uid, "authkey": authkey, "av_password": pw}

    # -- step 2: Ayla SSO mini token ---------------------------------------
    def _mini(self, jwt: str) -> str:
        self.log("[2/4] GET ayla-sso mini token …")
        r = self.s.get(self.cfg["mini"], headers={"Authorization": jwt}, timeout=20)
        if r.status_code != 200:
            self.log(f"      mini HTTP {r.status_code}: {r.text[:300]}")
            raise OwletError(f"ayla-sso mini failed ({r.status_code})")
        mini = r.json().get("mini_token") or r.json().get("token")
        if not mini:
            raise OwletError("no mini_token in response")
        self.log(f"      OK mini_token={_mask(mini)}")
        return mini

    # -- step 3: Ayla sign-in ----------------------------------------------
    def _sign_in(self, mini: str) -> str:
        self.log("[3/4] Ayla token_sign_in …")
        r = self.s.post(
            self.cfg["sign_in"],
            json={"app_id": self.cfg["app_id"], "app_secret": self.cfg["app_secret"],
                  "provider": "owl_id", "token": mini},
            timeout=20,
        )
        if r.status_code != 200:
            self.log(f"      sign_in HTTP {r.status_code}: {r.text[:300]}")
            raise OwletError(f"Ayla sign_in failed ({r.status_code})")
        tok = r.json().get("access_token")
        if not tok:
            raise OwletError("no access_token from Ayla")
        self.access_token = tok
        self.log(f"      OK access_token={_mask(tok)}")
        return tok

    def authenticate(self) -> str:
        return self._sign_in(self._mini(self._firebase()))

    # -- Ayla data ----------------------------------------------------------
    def _ayla_get(self, path: str) -> Any:
        url = f"{self.cfg['base']}{path}"
        r = self.s.get(url, headers={"Authorization": f"auth_token {self.access_token}"},
                       timeout=20)
        self.log(f"      GET {path} -> HTTP {r.status_code}")
        r.raise_for_status()
        return r.json()

    def devices(self) -> list[dict]:
        data = self._ayla_get("/devices.json")
        return [d.get("device", d) for d in data] if isinstance(data, list) else []

    def activate(self, dsn: str) -> bool:
        """POST APP_ACTIVE=1 so the Smart Sock streams live vitals.

        The sock only publishes fresh REAL_TIME_VITALS while the app (or us)
        pokes this datapoint; without it the properties go stale. Must be
        called right before each properties read (pyowletapi does this on
        every poll). Returns True on success.
        """
        if not self.access_token:
            self.authenticate()
        url = f"{self.cfg['base']}/dsns/{dsn}/properties/APP_ACTIVE/datapoints.json"
        try:
            r = self.s.post(
                url,
                headers={"Authorization": f"auth_token {self.access_token}"},
                json={"datapoint": {"metadata": {}, "value": 1}},
                timeout=15,
            )
            return r.status_code in (200, 201)
        except requests.RequestException as e:
            self.log(f"      APP_ACTIVE({dsn}) error: {e}")
            return False

    # -- the important bit: dump everything, hunt for the cam ---------------
    def diagnose(self) -> dict:
        """Full login + dump of all account data. Returns parsed candidates."""
        self.authenticate()
        self.log("[4/4] Listing devices …")
        try:
            devices = self.devices()
        except Exception as e:  # noqa: BLE001
            self.log(f"      devices.json error: {e}")
            devices = []

        self.log(f"      account has {len(devices)} Ayla device(s)")
        candidates: list[dict] = []
        for d in devices:
            dsn = d.get("dsn")
            model = d.get("oem_model") or d.get("model")
            name = d.get("product_name") or d.get("device_type")
            self.log(f"\n      ── device dsn={dsn} model={model} name={name}")
            # Don't dump the whole device object — it can carry tokens/secrets.
            # The curated credential candidates below surface what's actually needed.
            self.log("      fields: " + ", ".join(sorted(map(str, d.keys())))[:400])
            candidates += _hunt(d, f"device[{dsn}]")
            # Pull its properties too — cam params sometimes hide here.
            if dsn:
                try:
                    props = self._ayla_get(f"/dsns/{dsn}/properties.json")
                    names = [p.get("property", {}).get("name") for p in props] \
                        if isinstance(props, list) else []
                    self.log(f"      properties: {names}")
                    candidates += _hunt(props, f"props[{dsn}]")
                except Exception as e:  # noqa: BLE001
                    self.log(f"      properties error: {e}")

        if not devices:
            self.log("\n      NOTE: no devices on Ayla. The Cam likely registers on a")
            self.log("      different Owlet backend than the Sock. Share this log and")
            self.log("      we'll target the camera endpoint next.")

        # de-dup
        seen, uniq = set(), []
        for c in candidates:
            k = (c["field"], c["value"])
            if k not in seen:
                seen.add(k)
                uniq.append(c)
        self.log(f"\n[done] {len(uniq)} credential candidate(s) flagged.")
        return {"devices": len(devices), "candidates": uniq}


def _hunt(obj: Any, path: str = "") -> list[dict]:
    out = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            here = f"{path}.{k}" if path else str(k)
            if CRED_KEYS.search(str(k)) and not isinstance(v, (dict, list)):
                out.append({"field": here, "value": str(v)[:200]})
            out.extend(_hunt(v, here))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            out.extend(_hunt(v, f"{path}[{i}]"))
    elif isinstance(obj, str):
        for m in set(UID_RE.findall(obj)):
            out.append({"field": f"{path}<uid?>", "value": m})
    return out
