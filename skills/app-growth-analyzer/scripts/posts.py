#!/usr/bin/env python3
"""posts.py — every post of your own TikTok accounts, matched to the carousel it came from, scored.

    python3 posts.py pull                          # refresh <data>/posts.csv (~5-20 s per account)
    python3 posts.py pull --captions 'tips=renders/tips/*/caption.txt'
    python3 posts.py report                        # accounts, best / worst, by format, last 30 days
    python3 posts.py report --days 14 --min-age 3 --by sound

pull: yt-dlp reads each public profile in the accounts file (--flat-playlist, no login, no key)
and returns every post with views, likes, comments, shares, saves, sound and hashtags.

Match: a post is tied to its rendered folder by its EXACT hashtag set — each caption file you
posted must carry a set no other caption has. The match is stored on the post the first time a
pull sees it, so it survives you deleting old render folders later. Pull daily for that reason.
--captions (or POSTS_CAPTIONS, ';'-separated) = 'format=glob' or a bare glob, where the format
is then the name of the folder two levels above the caption file.

Score = views / the median views of the SAME account's posts in the window. That cancels
account luck: 2,000 views is a win on a 900-median account, a loss on a 6,000 one. Only
posts at least --min-age days old count, so a post still being distributed is not judged.
"""

import argparse
import csv
import glob
import json
import os
import re
import statistics
import subprocess
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from common import accounts, data_dir, load_env

FIELDS = ["id", "account", "posted", "views", "likes", "comments", "shares", "saves",
          "sound", "hashtags", "carousel", "url", "pulled"]
HISTORY_FIELDS = ["pulled", "id", "age_h", "views", "likes", "shares", "saves"]
HISTORY_DAYS = 14     # young posts get one history row per pull: views at a fixed age, later
ACTS = ("likes", "comments", "shares", "saves")
DAY = 86400


def tags(text):
    return frozenset(t.lower() for t in re.findall(r"#\w+", text or ""))


def captions(specs):
    """{hashtag set: "format/folder"} for every caption file the specs point at."""
    index = {}
    for spec in specs:
        name, _, pattern = spec.partition("=") if "=" in spec else ("", "", spec)
        for path in glob.glob(os.path.expanduser(pattern)):
            p = Path(path)
            key = tags(p.read_text())
            if key:
                index[key] = f"{name or p.parent.parent.name}/{p.parent.name}"
    return index


def fetch(account, max_posts=None):
    """one profile's posts, or the reason there are none (private, renamed, blocked)."""
    handle, _, uid = account
    url = f"tiktokuser:{uid}" if uid else f"https://www.tiktok.com/@{handle}"
    limit = ["--playlist-end", str(max_posts)] if max_posts else []
    run = subprocess.run(["yt-dlp", "--no-update", "--flat-playlist", "-J", *limit, url],
                         capture_output=True, text=True)
    if run.returncode:
        errors = [l for l in run.stderr.splitlines() if l.startswith("ERROR")]
        return handle, None, errors[-1] if errors else run.stderr.strip()[-200:]
    return handle, json.loads(run.stdout).get("entries") or [], None


def load(data):
    p = Path(data) / "posts.csv"
    if not p.exists():
        return {}
    with p.open() as f:
        return {r["id"]: r for r in csv.DictReader(f)}


def pull(data, accounts_file=None, specs=(), max_accounts=None, max_posts=None):
    """refresh posts.csv. An account dropped from the accounts file keeps its old rows:
    posts.csv is the whole history. Returns the handles that could not be read."""
    accs = accounts(accounts_file)[:max_accounts]
    if not accs:
        sys.exit("no accounts — one handle per line in accounts.txt (or ACCOUNTS_FILE / --accounts)")
    posts, index, now = load(data), captions(specs), time.time()
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    history, failed = [], []
    with ThreadPoolExecutor(6) as pool:
        results = list(pool.map(lambda acc: fetch(acc, max_posts), accs))
    for handle, entries, error in results:
        if entries is None:
            print(f"  {handle:26} FAILED  {error}")
            failed.append(handle)
            continue
        new = 0
        for e in entries:
            if not e.get("timestamp"):
                continue
            old = posts.get(e["id"], {})
            new += not old
            hashtags = tags(e.get("description"))
            posts[e["id"]] = row = {
                "id": e["id"], "account": handle,
                "posted": datetime.fromtimestamp(e["timestamp"], timezone.utc).strftime("%Y-%m-%d %H:%M"),
                "views": e.get("view_count") or 0, "likes": e.get("like_count") or 0,
                "comments": e.get("comment_count") or 0, "shares": e.get("repost_count") or 0,
                "saves": e.get("save_count") or 0, "sound": e.get("track") or "",
                "hashtags": " ".join(sorted(hashtags)),
                "carousel": old.get("carousel") or index.get(hashtags, ""),
                "url": e.get("url") or e.get("webpage_url") or f"https://www.tiktok.com/@{handle}/video/{e['id']}",
                "pulled": stamp,
            }
            if now - e["timestamp"] < HISTORY_DAYS * DAY:
                history.append({"pulled": stamp, "id": e["id"], "age_h": round((now - e["timestamp"]) / 3600),
                                **{k: row[k] for k in ("views", "likes", "shares", "saves")}})
        last = max((e.get("timestamp") or 0 for e in entries), default=0)
        quiet = f"  last post {int((now - last) / DAY)}d ago" if last and now - last > 2 * DAY else ""
        print(f"  {handle:26} {len(entries):4} posts  +{new} new{quiet}")
    path = Path(data) / "posts.csv"
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(sorted(posts.values(), key=lambda r: r["posted"], reverse=True))
    hist = Path(data) / "history.csv"
    fresh = not hist.exists()
    with hist.open("a", newline="") as f:
        w = csv.DictWriter(f, HISTORY_FIELDS)
        if fresh:
            w.writeheader()
        w.writerows(history)
    print(f"{len(posts)} posts in {path}, {sum(bool(r['carousel']) for r in posts.values())} matched")
    return failed


def fmt(carousel):
    """"tips/5-apps-0412" -> "tips"; unmatched -> "unmatched"."""
    return carousel.partition("/")[0] if carousel else "unmatched"


def age_days(r, now=None):
    t = datetime.strptime(r["posted"], "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc).timestamp()
    return ((now or time.time()) - t) / DAY


def activity(rs):
    """share of views that did something (like, comment, share, save), pooled over the posts —
    summed, then divided, so one 40-view post cannot swing it."""
    views = sum(int(r["views"] or 0) for r in rs)
    return sum(int(r[k] or 0) for r in rs for k in ACTS) / views if views else None


def scored(posts, min_age=3, max_age=30, now=None):
    """posts in the age window, each with score = views / its account's median in that window."""
    window = [dict(r, views=int(r["views"] or 0)) for r in posts if min_age <= age_days(r, now) <= max_age]
    by = defaultdict(list)
    for r in window:
        by[r["account"]].append(r["views"])
    med = {a: statistics.median(v) for a, v in by.items()}
    for r in window:
        r["score"] = r["views"] / med[r["account"]] if med[r["account"]] else 0.0
    return window, med


def group(rs, key):
    """[(value, posts, median score, beat-median share, activity)], best first."""
    g = defaultdict(list)
    for r in rs:
        g[key(r)].append(r)
    out = [(v, len(x), statistics.median(r["score"] for r in x),
            sum(r["score"] > 1 for r in x) / len(x), activity(x)) for v, x in g.items()]
    return sorted(out, key=lambda t: -t[2])


def report(a):
    posts = list(load(a.data).values())
    if not posts:
        sys.exit("no posts yet — run: python3 posts.py pull")
    window, med = scored(posts, a.min_age, a.days)
    print(f"posts {a.min_age}-{a.days} days old: {len(window)}, {sum(bool(r['carousel']) for r in window)} matched\n")
    print(f"{'account':26} {'posts':>5} {'median':>7} {'best':>7}")
    for acc in sorted(med, key=med.get, reverse=True):
        vs = [r["views"] for r in window if r["account"] == acc]
        print(f"{acc:26} {len(vs):5} {int(med[acc]):7} {max(vs):7}")
    silent = {h for h, _, _ in accounts(a.accounts)} - set(med)
    if silent:
        print(f"no posts in window: {', '.join(sorted(silent))}")
    ranked = sorted((r for r in window if r["carousel"]), key=lambda r: -r["score"])
    for title, rs in (("best", ranked[:a.top]), ("worst", ranked[::-1][:a.top])):
        print(f"\n{title} (score = views / account median · activity)")
        for r in rs:
            print(f"  {r['score']:5.2f}x {r['views']:8} {activity([r]) or 0:6.1%}  {r['account']:22} {r['carousel']}")
    for field in a.by:
        key = (lambda r: fmt(r["carousel"])) if field == "format" else (lambda r, f=field: r.get(f) or "?")
        print(f"\nby {field}")
        print(f"  {'value':34} {'n':>5} {'median':>7} {'beat':>5} {'active':>7}")
        for v, n, m, beat, act in group(window, key):
            print(f"  {str(v)[:34]:34} {n:5} {m:6.2f}x {beat:5.0%} {act or 0:7.1%}")
    print("\n  a 2x gap needs ~48 posts per value to read (default, assumes sd of log score ~1.2);"
          " smaller gaps need far more")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0], epilog=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", help="data folder (default GROWTH_DATA or ./growth-data)")
    ap.add_argument("--accounts", help="accounts file (default ACCOUNTS_FILE or ./accounts.txt)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("pull", help="refresh posts.csv from TikTok")
    p.add_argument("--captions", action="append", default=[],
                   help="'format=glob' or glob of caption files (repeatable; default POSTS_CAPTIONS)")
    p.add_argument("--max-accounts", type=int, help="only the first N accounts (a quick test)")
    p.add_argument("--max-posts", type=int, help="only the newest N posts per account (a quick test)")
    r = sub.add_parser("report", help="print scores")
    r.add_argument("--days", type=int, default=30)
    r.add_argument("--min-age", type=int, default=3)
    r.add_argument("--top", type=int, default=5)
    r.add_argument("--by", nargs="*", default=["format"], help="format, sound, account (default format)")
    a = ap.parse_args()
    load_env()
    a.data = data_dir(a.data)
    if a.cmd == "pull":
        specs = a.captions or [s for s in os.environ.get("POSTS_CAPTIONS", "").split(";") if s.strip()]
        pull(a.data, a.accounts, specs, a.max_accounts, a.max_posts)
    else:
        report(a)


if __name__ == "__main__":
    main()
