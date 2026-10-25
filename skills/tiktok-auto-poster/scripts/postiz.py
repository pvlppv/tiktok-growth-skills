#!/usr/bin/env python3
"""Postiz Cloud client: book a carousel as a TikTok INBOX DRAFT (photos + title + hashtags).

Used by `poster.py postiz`. On its own it only reads:

    postiz.py channels        # connected, enabled channels: handle -> provider
    postiz.py check --days 2  # recent posts, failures first

Key: POSTIZ_API_KEY in .env. API: https://docs.postiz.com/public-api
(Authorization header = the raw key; POST /upload, POST /posts, GET /integrations, GET /posts).
Set POSTIZ_API_URL for a self-hosted instance (https://<your-domain>/api/public/v1) — but read
references/postiz.md first: self-hosted cannot publish public TikToks.
"""

import argparse
import io
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from PIL import Image, ImageOps

UTC = ZoneInfo("UTC")
LEAD_MIN = 10          # a slot closer than this is pushed later: Postiz needs time to pull photos
SHORT_SIDE_MAX = 1080  # TikTok photo posts: shorter side at most 1080 px
TITLE_MAX = 90         # TikTok title cap
PHOTOS_MAX = 35        # TikTok photo post cap


def api_url():
    return os.environ.get("POSTIZ_API_URL", "https://api.postiz.com/public/v1").rstrip("/")


def call(method, path, tries=4, **kw):
    """one call; retries 429/5xx and dropped links, raises on anything else. Never prints the key."""
    key = os.environ.get("POSTIZ_API_KEY") or sys.exit("POSTIZ_API_KEY missing in .env")
    err = None
    for attempt in range(tries):
        try:
            r = requests.request(method, api_url() + path, headers={"Authorization": key},
                                 timeout=120, **kw)
        except requests.exceptions.RequestException as e:
            err = type(e).__name__
        else:
            if r.ok:
                return r.json()
            if r.status_code != 429 and r.status_code < 500:
                raise RuntimeError(f"{method} {path} {r.status_code}: {r.text[:300]}")
            err = str(r.status_code)
        if attempt + 1 < tries:
            time.sleep(min(2 ** attempt * 5, 60))
    raise RuntimeError(f"{method} {path} failed after {tries} tries ({err})")


def channels():
    """{handle: integration} for every connected, enabled channel (handle = Postiz `profile`)."""
    return {i["profile"].lstrip("@"): i for i in call("GET", "/integrations") if not i.get("disabled")}


def jpeg(path):
    """TikTok photo posts take JPEG/WebP only, shorter side <= 1080. Re-encoding also drops
    all metadata — PIL writes none unless asked."""
    im = ImageOps.exif_transpose(Image.open(path)).convert("RGB")
    s = SHORT_SIDE_MAX / min(im.size)
    if s < 1:
        im = im.resize((round(im.width * s), round(im.height * s)), Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=92)
    return buf.getvalue()


def slot(day, at, tz):
    """`day` at `at` (HH:MM) in `tz`; a slot already gone moves to LEAD_MIN from now.
    No jitter on purpose: a draft goes live when a person taps Post, not when it lands."""
    h, m = map(int, at.split(":"))
    when = datetime(day.year, day.month, day.day, h, m, tzinfo=ZoneInfo(tz))
    return max(when, datetime.now(ZoneInfo(tz)) + timedelta(minutes=LEAD_MIN))


def book_draft(handle, d, when, chans):
    """upload folder `d`'s images and schedule them as a draft on `handle`. Raises on any
    failure so the caller never marks a half-done post as booked."""
    from poster import caption, images
    if handle not in chans:
        raise RuntimeError(f"@{handle} is not connected (or disabled) in Postiz")
    pics = images(d)
    if not pics or len(pics) > PHOTOS_MAX:
        raise RuntimeError(f"{len(pics)} images — TikTok takes 1-{PHOTOS_MAX}")
    title, rest = caption(d)
    media = []
    for p in pics:
        u = call("POST", "/upload", files={"file": (p.stem + ".jpg", jpeg(p), "image/jpeg")})
        media.append({"id": u["id"], "path": u["path"]})
    body = {
        "type": "schedule",
        "date": when.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "shortLink": False,
        "tags": [],
        "posts": [{
            "integration": {"id": chans[handle]["id"]},
            "value": [{"content": rest, "image": media}],
            # required by the Postiz schema. In an UPLOAD (inbox draft) TikTok keeps only the
            # title and description; the rest is chosen on the phone when the draft is posted.
            "settings": {
                "__type": chans[handle]["identifier"],
                "title": title[:TITLE_MAX],
                "privacy_level": "PUBLIC_TO_EVERYONE",
                "duet": False,
                "stitch": False,
                "comment": True,
                "autoAddMusic": "no",
                "brand_content_toggle": False,
                "brand_organic_toggle": False,
                "video_made_with_ai": False,
                "content_posting_method": "UPLOAD",
            },
        }],
    }
    # ONE try: a 5xx can still have created the post, and a retry would book it twice
    return call("POST", "/posts", tries=1, json=body)


def check(days=2):
    now = datetime.now(UTC)
    fmt = "%Y-%m-%dT%H:%M:%S.000Z"
    res = call("GET", "/posts", params={"startDate": (now - timedelta(days=days)).strftime(fmt),
                                        "endDate": (now + timedelta(days=1)).strftime(fmt)})
    posts = res.get("posts", []) if isinstance(res, dict) else res
    name = {i["id"]: h for h, i in channels().items()}
    for p in sorted(posts, key=lambda p: (p.get("state") != "ERROR", p.get("publishDate", ""))):
        integ = p.get("integration") or {}
        print(f"{p.get('state', '?'):10} {p.get('publishDate', '?')[:16]}Z  "
              f"@{name.get(integ.get('id'), integ.get('name', '?')):<24} {p.get('releaseURL') or ''}"
              f"{'  ' + p['error'] if p.get('error') else ''}")
    if not posts:
        print("no posts in that window")


def main():
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from poster import load_env
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--env", help="path to .env")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("channels", help="list connected channels")
    c = sub.add_parser("check", help="recent posts, failures first")
    c.add_argument("--days", type=int, default=2)
    args = ap.parse_args()
    load_env(args.env)
    if args.cmd == "channels":
        for h, i in channels().items():
            print(f"@{h:<26} {i.get('identifier')}")
    else:
        check(args.days)


if __name__ == "__main__":
    main()
