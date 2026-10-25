"""tt.py — the small shared helpers every command here uses. Not a command itself.

yt-dlp   free per-post stats of any public profile (no login, no key)
tikwm    free slide image links, comments, hashtag pages (1 request/s, ~10k calls/day shared)
Apify    paid TikTok search (only find.py search uses it, always under --max-usd)
"""
import json
import os
import re
import statistics
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from io import BytesIO
from pathlib import Path

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0 Safari/537.36")
DAY = 86400


def env(key):
    """a key from the environment, else from .env in the working dir or the skill folder."""
    if os.environ.get(key):
        return os.environ[key]
    for f in (Path.cwd() / ".env", Path(__file__).resolve().parents[1] / ".env"):
        if f.exists():
            for line in f.read_text().splitlines():
                if line.startswith(key + "="):
                    return line.split("=", 1)[1].strip().strip('"')
    sys.exit(f"{key} missing: export it or put it in .env (see .env.example)")


def median(xs):
    xs = [x for x in xs if x is not None]
    return statistics.median(xs) if xs else None


def handle_of(s):
    """'@name', 'name' or a profile/post URL -> 'name'."""
    s = s.strip()
    m = re.search(r"/@([\w.\-]+)", s)
    return m.group(1) if m else s.lstrip("@")


# ---------- yt-dlp: post list + stats, free ----------

def ytdlp_posts(handle, cap=None):
    """(entries, error). Each entry: id, url, timestamp, view_count, like_count, comment_count,
    repost_count, save_count, track, description (hashtags, not the full caption), thumbnails.
    A handle TikTok refuses by name can be read as 'tiktokuser:<numeric id>'."""
    url = handle if handle.startswith("tiktokuser:") else f"https://www.tiktok.com/@{handle}"
    cmd = ["yt-dlp", "--no-update", "--flat-playlist", "-J", url]
    if cap:
        cmd[3:3] = ["--playlist-end", str(cap)]
    try:
        run = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    except FileNotFoundError:
        sys.exit("yt-dlp not found: pip install yt-dlp  (or brew install yt-dlp)")
    except subprocess.TimeoutExpired:
        return None, "timeout"
    if run.returncode:
        errs = [l for l in run.stderr.splitlines() if "ERROR" in l]
        return None, (errs[-1] if errs else run.stderr.strip()[-200:])
    return json.loads(run.stdout).get("entries") or [], None


def is_carousel(entry):
    """photo carousel: yt-dlp's thumbnail URL says 'photomode' (yt-dlp itself returns no slides)."""
    return "photomode" in " ".join(t.get("url", "") for t in entry.get("thumbnails") or [])


def profile_stats(handle):
    """exact follower / like / video totals from the public profile HTML, or None."""
    try:
        req = urllib.request.Request(f"https://www.tiktok.com/@{handle}", headers={"User-Agent": UA})
        html = urllib.request.urlopen(req, timeout=20).read().decode()
        blob = re.search(r'id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>(.*?)</script>', html, re.S).group(1)
        u = json.loads(blob)["__DEFAULT_SCOPE__"]["webapp.user-detail"]["userInfo"]
        s = u["statsV2"]
        return {"followers": int(s["followerCount"]), "likes_total": int(s["heartCount"]),
                "videos": int(s["videoCount"]), "bio": u.get("user", {}).get("signature", ""),
                "bio_link": (u.get("user", {}).get("bioLink") or {}).get("link", "")}
    except Exception:
        return None


# ---------- tikwm: slides, comments, hashtags, free ----------

_gate, _last = threading.Lock(), [0.0]


def tikwm(path, **params):
    """GET https://www.tikwm.com/api/<path>; returns data or None. Paced to 1 request/s across
    threads (the free limit). The ~10,000 calls/day cap is shared by everyone on your IP."""
    url = "https://www.tikwm.com/api/" + path + "?" + urllib.parse.urlencode(params)
    for attempt in range(4):
        with _gate:
            wait = _last[0] + 1.1 - time.time()
            if wait > 0:
                time.sleep(wait)
            _last[0] = time.time()
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            d = json.loads(urllib.request.urlopen(req, timeout=40).read())
            if d.get("code") == 0:
                return d.get("data")
            msg = d.get("msg", "")
            if "limit" in msg.lower():
                print(f"tikwm: {msg} (daily cap? it resets ~00:00 UTC)", file=sys.stderr)
        except Exception as e:
            msg = str(e)
        time.sleep(3 + attempt * 3)
    print(f"tikwm gave up on {path}: {msg}", file=sys.stderr)
    return None


def download(url, path):
    if not path.exists():
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        path.write_bytes(urllib.request.urlopen(req, timeout=40).read())
    return path


# ---------- Apify: paid search ----------

APIFY = "https://api.apify.com/v2"


def _http(url, payload=None, timeout=300):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code < 500:  # Apify says why in the body; the token is never printed
                sys.exit(f"Apify {e.code}: {e.read().decode()[:300]}")
        except Exception as e:
            print(f"retry {attempt}: {e}", file=sys.stderr)
        time.sleep(5 + attempt * 5)
    sys.exit("Apify unreachable")


def apify_run(actor, run_input, max_usd):
    """one Apify actor run, capped on Apify's side by maxTotalChargeUsd. Returns (items, usd)."""
    tok = env("APIFY_TOKEN")
    r = _http(f"{APIFY}/acts/{actor}/runs?token={tok}&maxTotalChargeUsd={max_usd}", run_input)["data"]
    while r["status"] in ("READY", "RUNNING"):
        time.sleep(10)
        r = _http(f"{APIFY}/actor-runs/{r['id']}?token={tok}")["data"]
    items = _http(f"{APIFY}/datasets/{r['defaultDatasetId']}/items?token={tok}&clean=true")
    time.sleep(10)  # the run's own cost settles a few seconds after it ends
    usd = _http(f"{APIFY}/actor-runs/{r['id']}?token={tok}")["data"].get("usageTotalUsd") or 0
    print(f"Apify {r['status']}: {len(items)} items, run cost ${usd:.3f}", file=sys.stderr)
    return items, usd


# ---------- contact sheets ----------

def sheet(images, path, slide_w=360, cols=3, title=None):
    """slides in a grid, slide 1 top-left. images: local paths or URLs. True if written."""
    from PIL import Image, ImageDraw
    ims = []
    for src in images:
        try:
            if str(src).startswith("http"):
                req = urllib.request.Request(str(src), headers={"User-Agent": "Mozilla/5.0"})
                im = Image.open(BytesIO(urllib.request.urlopen(req, timeout=30).read()))
            else:
                im = Image.open(src)
            im = im.convert("RGB")
            ims.append(im.resize((slide_w, round(slide_w * im.height / im.width))))
        except Exception as e:
            print(f"  slide skipped: {e}", file=sys.stderr)
    if not ims:
        return False
    gap, head = 6, (40 if title else 0)
    cols = min(cols, len(ims))
    h = max(i.height for i in ims)
    rows = -(-len(ims) // cols)
    out = Image.new("RGB", (cols * (slide_w + gap) - gap, head + rows * (h + gap) - gap), "white")
    if title:
        ImageDraw.Draw(out).text((10, 12), title[:120], fill="black")
    for n, im in enumerate(ims):
        out.paste(im, ((n % cols) * (slide_w + gap), head + (n // cols) * (h + gap)))
    out.save(path, quality=85)
    return True
