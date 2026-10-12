#!/usr/bin/env python3
"""
lib_extract.py — self-provision the proprietary TUTK .so libraries.

The libraries are NOT shipped with this project (they're ThroughTek/Owlet's). The
user drops the Owlet app they downloaded — an .apk, or an .apkm/.xapk bundle of
split APKs — into the config folder, and this pulls the five x86_64 TUTK
libraries out of it on startup (or on demand from the web UI).

Used by both start-bionic.sh (auto on boot) and webapp.py (the "Extract" button).
Stdlib only.
"""

from __future__ import annotations

import glob
import io
import os
import zipfile

ARCH = "x86_64"
TUTK_LIBS = [
    "libIOTCAPIs.so",
    "libAVAPIs.so",
    "libTUTKGlobalAPIs.so",
    "libP2PTunnelAPIs.so",
    "libRDTAPIs.so",
]
# Optional deps we copy too if present (harmless if absent).
EXTRA_LIBS = ["libc++_shared.so", "libcrypto.so", "libssl.so", "libcurl.so"]

DEFAULT_SEARCH = ["/config", "/config/apk", "/app/libs", "/apk"]


def find_archives(dirs) -> list[str]:
    out: list[str] = []
    for d in dirs:
        if not d or not os.path.isdir(d):
            continue
        for ext in ("*.apk", "*.apkm", "*.xapk", "*.apks", "*.zip"):
            out += glob.glob(os.path.join(d, "**", ext), recursive=True)
    # de-dupe, stable order, prefer bundles that likely hold an x86_64 split last-named
    return sorted(set(out))


def _libs_in_zip(zf: zipfile.ZipFile) -> dict[str, bytes]:
    wanted = set(TUTK_LIBS) | set(EXTRA_LIBS)
    out: dict[str, bytes] = {}
    for n in zf.namelist():
        norm = n.replace("\\", "/")
        base = os.path.basename(norm)
        if norm.startswith(f"lib/{ARCH}/") and base in wanted:
            out[base] = zf.read(n)
    return out


def _libs_from_archive(path: str) -> dict[str, bytes]:
    """Find the TUTK libs in an .apk (direct) or .apkm/.xapk (split bundle)."""
    with zipfile.ZipFile(path) as z:
        libs = _libs_in_zip(z)
        if any(l in libs for l in TUTK_LIBS):
            return libs
        # bundle of split APKs — open the inner apks, prefer an x86_64 split
        inner = [n for n in z.namelist() if n.lower().endswith(".apk")]
        inner.sort(key=lambda n: (0 if "x86_64" in n.lower() else 1, n))
        for n in inner:
            try:
                with zipfile.ZipFile(io.BytesIO(z.read(n))) as iz:
                    libs = _libs_in_zip(iz)
                    if any(l in libs for l in TUTK_LIBS):
                        return libs
            except zipfile.BadZipFile:
                continue
    return {}


def have_all(lib_dir: str) -> bool:
    return all(os.path.exists(os.path.join(lib_dir, l)) for l in TUTK_LIBS)


def provision(lib_dir: str, search_dirs=None, log=print) -> tuple[bool, str]:
    """Ensure the five TUTK libs exist in lib_dir, extracting from a dropped APK
    if needed. Returns (ok, human message)."""
    search_dirs = search_dirs or DEFAULT_SEARCH
    try:
        os.makedirs(lib_dir, exist_ok=True)
    except OSError as e:
        return False, f"cannot create {lib_dir}: {e.strerror}"
    if have_all(lib_dir):
        return True, "TUTK libraries already present"

    archives = find_archives(search_dirs)
    if not archives:
        return False, ("no Owlet APK found. Put your Owlet .apkm (or .apk) in the "
                       "config folder and try again.")
    for apk in archives:
        try:
            libs = _libs_from_archive(apk)
        except (zipfile.BadZipFile, OSError) as e:
            log(f"  {os.path.basename(apk)}: {e}")
            continue
        if all(l in libs for l in TUTK_LIBS):
            for name, blob in libs.items():
                try:
                    with open(os.path.join(lib_dir, name), "wb") as fh:
                        fh.write(blob)
                except OSError as e:
                    return False, f"cannot write {lib_dir}: {e.strerror}"
            return True, (f"extracted {len(libs)} libraries from "
                          f"{os.path.basename(apk)}")
    return False, ("found an archive but it had no x86_64 TUTK libraries — make "
                   "sure it's the Owlet app with an x86_64 split.")


if __name__ == "__main__":
    import sys
    d = os.environ.get("TUTK_LIB_DIR", "/app/libs/x86_64")
    ok, msg = provision(d)
    print(f"[lib_extract] {msg}")
    sys.exit(0 if ok else 1)
