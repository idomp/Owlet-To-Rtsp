#!/usr/bin/env python3
"""
render_streams.py — (re)generate the go2rtc config + per-camera env files from the
saved settings. Run at container boot by start-bionic.sh.

  render_streams.py            generate /config/go2rtc.gen.yaml + /config/cameras/*.env
  render_streams.py --names    print the camera/stream names, one per line

Exit non-zero if the config folder isn't writable, so start-bionic.sh can fall
back to the baked-in single-camera config.
"""

from __future__ import annotations

import sys

import config_store as cs


def main() -> int:
    cfg = cs.load_config()
    if "--names" in sys.argv:
        print("\n".join(cs.camera_names(cfg)))
        return 0
    # OWLET_FORGET_ACCOUNT=1: drop stored account credentials at boot once every
    # camera has its key, so the env files below are written without them.
    if cs.forget_account_if_keyed(cfg):
        if cs.save_config(cfg, regenerate=False):
            sys.stderr.write("[render] account email/password removed (OWLET_FORGET_ACCOUNT); "
                             "running on the saved camera keys\n")
    account = {k: cfg.get(k, cs.ACCOUNT_DEFAULTS[k]) for k in cs.ACCOUNT_FIELDS}
    cameras = cfg.get("cameras") or []
    try:
        path = cs.generate(account, cameras)
    except OSError as e:
        sys.stderr.write(f"[render] cannot write config ({e}); using baked-in default\n")
        return 1
    sys.stderr.write(f"[render] wrote {path} with {len(cameras) or 1} stream(s): "
                     f"{', '.join(cs.camera_names(cfg))}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
