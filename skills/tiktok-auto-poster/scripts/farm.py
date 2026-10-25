#!/usr/bin/env python3
"""iPhone farm client: turn each planned carousel into ONE one-shot task on its phone.

Talks to an iOS phone-farm server with a local HTTP API; anything serving these two routes works:

    GET  /api/devices                      -> [{name, udid, pluginData:{<plugin>:{accounts:[...]}}}]
    POST /api/devices/<udid>/posts         multipart: destination draft|publish, account, title,
                                           caption, timing {"kind":"once","runAt":ISO}, musicUrl?,
                                           media (repeated, one per slide)

Used by `poster.py farm`. On its own it only reads:

    farm.py devices          # phones the farm knows, with their TikTok accounts

FARM_URL (default http://127.0.0.1:3100) and FARM_PLUGIN (required: the key your farm uses under pluginData) in .env.
The device name in your accounts file must equal the farm's device name.
"""

import argparse
import json
import mimetypes
import os
import random
import sys
import urllib.parse
from datetime import datetime, timedelta
from pathlib import Path

import requests


def base():
    return os.environ.get("FARM_URL", "http://127.0.0.1:3100").rstrip("/")


def headers():
    return {"origin": base()}   # the farm rejects cross-origin writes


def devices():
    """{device name: {"udid", "accounts"}} for every phone with TikTok accounts configured."""
    plugin = os.environ.get("FARM_PLUGIN")
    if not plugin:
        sys.exit("FARM_PLUGIN is not set: the key your farm server uses under pluginData")
    r = requests.get(base() + "/api/devices", headers=headers(), timeout=30)
    r.raise_for_status()
    out = {}
    for d in r.json():
        accs = [a.lstrip("@") for a in d.get("pluginData", {}).get(plugin, {}).get("accounts", [])]
        if accs:
            out[d["name"]] = {"udid": d["udid"], "accounts": accs}
    return out


def clean_music(url):
    """TikTok share links carry a screenful of tracking query; the path is enough."""
    u = urllib.parse.urlparse(url.strip())
    if u.scheme != "https" or not u.netloc.endswith("tiktok.com"):
        sys.exit(f"not an https tiktok.com sound url: {url}")
    return urllib.parse.urlunparse(u._replace(query="", fragment=""))


def create_post(udid, d, account, when, music, publish):
    from poster import caption, images
    title, rest = caption(d)
    data = [("destination", "publish" if publish else "draft"), ("account", account),
            ("title", title), ("caption", rest),
            # always ONE-SHOT: a recurring task re-posts the same media every time it fires
            ("timing", json.dumps({"kind": "once", "runAt": when.isoformat()}))]
    if music:
        data.append(("musicUrl", music))
    files = [("media", (p.name, p.open("rb"), mimetypes.guess_type(p.name)[0] or "image/png"))
             for p in images(d)]
    try:
        r = requests.post(f"{base()}/api/devices/{udid}/posts", data=data, files=files,
                          headers=headers(), timeout=300)
    finally:
        for _, (_, fh, _) in files:
            fh.close()
    r.raise_for_status()
    return r.json()


def push(rows, args):
    """rows = [(plan, folder)] already filtered by poster.py. Prints the schedule; --live creates it."""
    from poster import DONE, caption, stamp
    music = [clean_music(u) for u in json.loads(args.music.read_text())] if args.music else []
    farm = devices() if args.live else None
    h, m = map(int, args.start.split(":"))
    first = datetime.fromisoformat(args.date).replace(hour=h, minute=m)
    if first < datetime.now():
        first = datetime.now() + timedelta(minutes=10)
    slot, last_sound = {}, {}
    for p, d in rows:
        n = slot[p["device"]] = slot.get(p["device"], -1) + 1
        rng = random.Random(f"{p['handle']}{args.date}{n}")   # seeded: dry run == live run
        # a fixed cadence across many phones is its own fingerprint, hence the jitter
        when = first + timedelta(minutes=n * args.gap + rng.randint(-args.jitter, args.jitter))
        pick = [s for s in music if s != last_sound.get(p["handle"])] or music
        sound = rng.choice(pick) if pick else None
        last_sound[p["handle"]] = sound
        line = f"{when:%m-%d %H:%M}  {p['device'][:14]:14} @{p['handle']:<22} {caption(d)[0][:40]}"
        if not args.live:
            print(f"  [dry] {line}")
            continue
        phone = farm.get(p["device"])
        if not phone or p["handle"] not in phone["accounts"]:
            print(f"  FARM SKIP {d.name}: farm has no device '{p['device']}' with @{p['handle']}")
            continue
        create_post(phone["udid"], d, p["handle"], when, sound, args.publish)
        stamp(d, DONE["farm"])
        print(f"  pushed {line}")
    mode = "POST PUBLICLY" if args.publish else "drafts"
    print(f"{len(rows)} task(s) as {mode}" + ("" if args.live else " [dry run — add --live]"))


def main():
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from poster import load_env
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--env", help="path to .env")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("devices", help="list farm phones and their TikTok accounts")
    args = ap.parse_args()
    load_env(args.env)
    for name, d in devices().items():
        print(f"{name:20} {d['udid'][:12]}...  {', '.join('@' + a for a in d['accounts'])}")


if __name__ == "__main__":
    main()
