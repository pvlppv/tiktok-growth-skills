#!/usr/bin/env python3
"""account.py — free stats for every post of any public TikTok account (yt-dlp, no login, no key).

    python3 account.py @handle1 handle2 https://www.tiktok.com/@handle3
    python3 account.py --file handles.txt --cap 600 --days 30

Per account: posts, carousels vs videos, posts/day, median views (all, last 30 d, first 30 d),
active (posted in 14 d) / stopped (nothing for 21+ d), months table, and its best posts by
score. score = the post's views / the median of the SAME account in the SAME calendar month
(5+ posts that month, else the account median): account hot spells swamp everything else.

Writes OUT/accounts/<handle>.json (raw entries, reused for 24 h), OUT/accounts.json,
OUT/posts.csv (one row per post), OUT/accounts.md.
"""
import argparse
import csv
import json
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tt  # noqa: E402

FIELDS = ["id", "account", "posted", "kind", "views", "likes", "comments", "shares", "saves",
          "score", "sound", "hashtags", "url"]


def fetch(handle, folder, cap):
    """entries for one account, cached for 24 h. Returns (handle, entries | None, error)."""
    f = folder / f"{handle}.json"
    if f.exists() and time.time() - f.stat().st_mtime < tt.DAY:
        d = json.loads(f.read_text())
        return handle, d.get("entries"), d.get("error")
    entries, err = tt.ytdlp_posts(handle, cap)
    f.write_text(json.dumps({"entries": entries, "error": err}, ensure_ascii=False))
    return handle, entries, err


def month(ts):
    return time.strftime("%Y-%m", time.gmtime(ts or 0))


def scores(entries):
    """post id -> views / same-account same-month median (fallback: account median)."""
    by_m = defaultdict(list)
    for e in entries:
        by_m[month(e.get("timestamp"))].append(e.get("view_count") or 0)
    acc = tt.median([e.get("view_count") or 0 for e in entries]) or 0
    out = {}
    for e in entries:
        vs = by_m[month(e.get("timestamp"))]
        m = tt.median(vs) if len(vs) >= 5 else acc
        out[e["id"]] = round((e.get("view_count") or 0) / m, 2) if m else None
    return out


def summary(handle, entries, now, cap):
    ts = sorted(e["timestamp"] for e in entries if e.get("timestamp"))
    if not ts:
        return {"posts": 0}
    first, last = ts[0], ts[-1]
    views = lambda es: tt.median([e.get("view_count") for e in es])
    span = max(1.0, (last - first) / tt.DAY)
    since = (now - last) / tt.DAY
    months = defaultdict(list)
    for e in entries:
        months[month(e.get("timestamp"))].append(e.get("view_count") or 0)
    return {
        "posts": len(entries), "capped": bool(cap) and len(entries) >= cap,
        "carousels": sum(map(tt.is_carousel, entries)),
        "first": time.strftime("%Y-%m-%d", time.gmtime(first)), "last": time.strftime("%Y-%m-%d", time.gmtime(last)),
        "days_since_last": round(since, 1), "active": since <= 14, "stopped": since > 21,
        "per_day": round(len(entries) / span, 2),
        "per_day_30d": round(sum(t > now - 30 * tt.DAY for t in ts) / 30, 2),
        "median_views": views(entries),
        "median_last30": views([e for e in entries if (e.get("timestamp") or 0) > last - 30 * tt.DAY]),
        "median_first30": views([e for e in entries if (e.get("timestamp") or 0) < first + 30 * tt.DAY]),
        "median_carousel": views([e for e in entries if tt.is_carousel(e)]),
        "median_video": views([e for e in entries if not tt.is_carousel(e)]),
        "months": {m: [len(v), tt.median(v)] for m, v in sorted(months.items())},
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("handles", nargs="*", help="@handle, handle, or profile URL")
    ap.add_argument("--file", help="handles, one per line or space-separated")
    ap.add_argument("--out", default="tiktok-research", help="working folder (default ./tiktok-research)")
    ap.add_argument("--cap", type=int, default=600, help="newest posts read per account (0 = all; big ones take minutes)")
    ap.add_argument("--days", type=int, default=30, help="window for the best-posts list")
    ap.add_argument("--top", type=int, default=5, help="best posts listed per account")
    ap.add_argument("--profile", action="store_true", help="also read followers / total likes / bio from the profile page")
    a = ap.parse_args()
    handles = [tt.handle_of(h) for h in a.handles + (Path(a.file).read_text().split() if a.file else [])]
    handles = list(dict.fromkeys(h for h in handles if h))
    if not handles:
        ap.error("give at least one handle")
    out = Path(a.out)
    (out / "accounts").mkdir(parents=True, exist_ok=True)
    now = time.time()
    with ThreadPoolExecutor(4) as ex:
        results = list(ex.map(lambda h: fetch(h, out / "accounts", a.cap or None), handles))

    accounts, posts, md = {}, [], []
    for handle, entries, err in results:
        if entries is None:
            print(f"  {handle:28} FAILED  {err}")
            accounts[handle] = {"error": err}
            continue
        s = summary(handle, entries, now, a.cap)
        if a.profile:
            s["profile"] = tt.profile_stats(handle)
        accounts[handle] = s
        sc = scores(entries)
        for e in entries:
            kind = "carousel" if tt.is_carousel(e) else "video"
            posts.append({
                "id": e["id"], "account": handle, "kind": kind, "ts": e.get("timestamp") or 0,
                "posted": time.strftime("%Y-%m-%d %H:%M", time.gmtime(e.get("timestamp") or 0)),
                "views": e.get("view_count") or 0, "likes": e.get("like_count") or 0,
                "comments": e.get("comment_count") or 0, "shares": e.get("repost_count") or 0,
                "saves": e.get("save_count") or 0, "score": sc.get(e["id"]), "sound": e.get("track") or "",
                "hashtags": " ".join(w for w in (e.get("description") or "").split() if w.startswith("#")),
                "url": f"https://www.tiktok.com/@{handle}/{'photo' if kind == 'carousel' else 'video'}/{e['id']}"})
        if not s["posts"]:
            print(f"  {handle:28} no posts")
            continue
        state = "active" if s["active"] else ("STOPPED" if s["stopped"] else "quiet")
        print(f"  {handle:28} {s['posts']:5} posts{'+' if s['capped'] else ' '} {s['carousels']:5} carousels  "
              f"median {s['median_views']:>9,.0f}  last30 {s['median_last30'] or 0:>9,.0f}  {s['per_day_30d']}/day  {state}")
        mine = [p for p in posts if p["account"] == handle and now - p["ts"] <= a.days * tt.DAY]
        best = sorted(mine, key=lambda p: -(p["score"] or 0))[:a.top]
        prof = s.get("profile") or {}
        md += [f"## @{handle}", "",
               f"{s['posts']} posts{' (cap hit: older ones not read)' if s['capped'] else ''}, {s['carousels']} carousels, "
               f"{s['first']} → {s['last']}, {s['per_day_30d']} posts/day last 30 d, {state}."
               + (f" Followers {prof['followers']:,}." if prof else ""), "",
               f"Median views: all {s['median_views']:,.0f} · last 30 d {s['median_last30'] or 0:,.0f} · "
               f"first 30 d {s['median_first30'] or 0:,.0f} · carousels {s['median_carousel'] or 0:,.0f} · "
               f"videos {s['median_video'] or 0:,.0f}", "",
               "| month | posts | median views |", "|---|---|---|"]
        md += [f"| {m} | {n} | {v:,.0f} |" for m, (n, v) in list(s["months"].items())[-12:]]
        md += ["", f"Best posts, last {a.days} d (score = views / same account same month median):", ""]
        md += [f"- {p['score']}x · {p['views']:,} views · {p['kind']} · {p['posted'][:10]} · {p['url']}" for p in best]
        md.append("")

    (out / "accounts.json").write_text(json.dumps(accounts, ensure_ascii=False, indent=1))
    with (out / "posts.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(sorted(posts, key=lambda p: p["posted"], reverse=True))
    (out / "accounts.md").write_text("# Account stats (yt-dlp, public)\n\n" + "\n".join(md))
    print(f"{len(posts)} posts -> {out / 'posts.csv'}, {out / 'accounts.md'}")


if __name__ == "__main__":
    main()
