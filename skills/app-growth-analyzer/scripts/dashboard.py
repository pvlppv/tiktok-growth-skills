#!/usr/bin/env python3
"""dashboard.py — the morning page: every account, views -> downloads per day, formats scored.

    python3 dashboard.py                 # pull posts + recent App Store days, write <data>/dashboard.md
    python3 dashboard.py --no-pull       # rebuild from what is saved, no network
    python3 dashboard.py --notion        # also sync the "All posts" database + replace a Notion page
    python3 dashboard.py --notion --dry  # say what Notion would get, write nothing

Where each number comes from:
- posts, views: posts.py pull (yt-dlp, accounts file)
- downloads:    App Store SALES report, ~1 day late (skipped when ASC keys are not set)
- impressions:  App Store analytics report, ~2 days late (only with ASC_ANALYTICS_REQUEST_ID)
- the App Store line: the newest pulse.py snapshot

Views of a day = views so far of the posts POSTED that day, so recent days still grow.
<data>/funnel.csv keeps every day; a day older than --freeze days is read back instead of
recomputed, so it survives hidden posts and a lost cache. The page is markdown that both a
viewer and Notion read (pipe tables).
"""

import argparse
import csv
import json
import os
import statistics
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import appstore
import posts as P
from common import accounts, data_dir, load_env

WEEK = 7
LOW = 0.3      # last 3 posts under 30% of the account's usual median = "low views" (shadowban sign)
NEED = 48      # posts per value to see a 2x gap (default, assumes sd of log score ~1.2)


def local(r):
    return datetime.strptime(r["posted"], "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc).astimezone()


def cell(v, f="{:,}"):
    return "—" if v is None else f.format(v)


def pct(share):
    return cell(share * 100 if share is not None else None, "{:.1f}%")


def table(head, rows):
    esc = lambda x: str(x).replace("|", "\\|")
    line = lambda cells: "| " + " | ".join(esc(c) for c in cells) + " |"
    return "\n".join([line(head), line(["---"] * len(head))] + [line(r) for r in rows])


def funnel(posts, today, data, days, freeze, fetch):
    """[(day, {posts, views, impressions, downloads})], newest first. None = not in yet."""
    path = data / "funnel.csv"
    stored = {}
    if path.exists():
        stored = {r["day"]: {k: int(v) if v else None for k, v in r.items() if k != "day"}
                  for r in csv.DictReader(path.open())}
    asc = fetch and all(os.environ.get(k) for k in ("ASC_KEY_ID", "ASC_ISSUER_ID", "ASC_P8", "ASC_VENDOR_NUMBER"))
    if asc:
        appstore.fetch_impressions(data)
    impressions = appstore.impressions_by_day(data)
    out = []
    for i in range(1, days + WEEK):
        d = today - timedelta(days=i)
        key = d.isoformat()
        if key in stored and i > freeze:
            out.append((d, stored[key]))
            continue
        mine = [r for r in posts if r["t"].date() == d]
        text = appstore.day("SALES", key, data, asc)
        dl = sum(int(r["Units"] or 0) for r in appstore.mine("SALES", appstore.rows(text))
                 if r["Product Type Identifier"] in appstore.DOWNLOAD)
        closed = text.strip() or (data / "raw" / f"SALES_{key}.tsv").exists()
        out.append((d, {"posts": len(mine), "views": sum(r["views"] for r in mine),
                        "impressions": impressions.get(key), "downloads": dl if closed else None}))
    merged = {**stored, **{d.isoformat(): r for d, r in out}}
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, ["day", "posts", "views", "impressions", "downloads"])
        w.writeheader()
        w.writerows({"day": k, **merged[k]} for k in sorted(merged))
    return out


def latest_snapshot(data):
    snaps = sorted((data / "snapshots").glob("[0-9]*.json"))
    return json.loads(snaps[-1].read_text()) if snaps else None


def build(data, failed=(), accounts_file=None, days=14, freeze=30, fetch=True, db_tag=None):
    posts = list(P.load(data).values())
    now = datetime.now().astimezone()
    today, yesterday = now.date(), now.date() - timedelta(days=1)
    for r in posts:
        r["views"], r["t"] = int(r["views"] or 0), local(r)
        r["age"] = (now - r["t"]).total_seconds() / 86400

    # --- funnel ---
    rows = funnel(posts, today, data, days, freeze, fetch)

    def per_1k(i):
        """downloads per 1k views over the WEEK ending that day. One day alone is noise:
        downloads keep coming from earlier posts and from people searching the app."""
        week = [r for _, r in rows[i:i + WEEK]]
        if len(week) < WEEK or any(r["downloads"] is None for r in week):
            return "—"
        v = sum(r["views"] for r in week)
        return f"{sum(r['downloads'] for r in week) / v * 1000:.2f}" if v else "—"
    funnel_rows = [[f"{d:%a %m-%d}", cell(r["posts"]), cell(r["views"]) if r["posts"] else "no posts",
                    pct(P.activity([p for p in posts if p["t"].date() == d])),
                    cell(r["impressions"]), cell(r["downloads"]), per_1k(i)]
                   for i, (d, r) in enumerate(rows[:days])]

    # --- accounts ---
    by = defaultdict(list)
    for r in posts:
        by[r["account"]].append(r)
    acc_rows, problems = [], []
    listed = accounts(accounts_file)
    for handle, label, _ in listed:
        rs = sorted(by[handle], key=lambda r: r["t"], reverse=True)
        last = (today - rs[0]["t"].date()).days if rs else None
        usual = [r["views"] for r in rs if 1 <= r["age"] <= 30]
        recent = [r["views"] for r in rs if r["age"] >= 1][:3]
        low = (len(usual) >= 5 and last is not None and last < 2
               and statistics.median(recent) < LOW * statistics.median(usual))
        status = ("can't read" if handle in failed else "no posts" if last is None
                  else f"quiet {last}d" if last >= 2 else "low views" if low else "ok")
        if status != "ok":
            problems.append(f"@{handle} {status}")
        acc_rows.append([f"[@{handle}](https://www.tiktok.com/@{handle})", label or "—",
                         cell(sum(r["t"].date() == yesterday for r in rs)), cell(last, "{}d"),
                         cell(int(statistics.median(usual)) if usual else None),
                         pct(P.activity([r for r in rs if 1 <= r["age"] <= 30])), status])

    # --- formats: score = views / that account's median, posts 3-30 days old ---
    window, _ = P.scored([{k: v for k, v in r.items() if k not in ("t", "age")} for r in posts])
    fmt_rows = [[v, n, f"{m:.2f}x", f"{beat:.0%}", pct(act), "ready" if n >= NEED else f"{NEED - n} more"]
                for v, n, m, beat, act in P.group(window, lambda r: P.fmt(r["carousel"]))]

    # --- page ---
    names = {h for h, _, _ in listed}
    week = [r for _, r in rows[:WEEK] if r["downloads"] is not None]
    wv, wd = sum(r["views"] for r in week), sum(r["downloads"] for r in week)
    posted_y = [r for r in posts if r["t"].date() == yesterday and r["account"] in names]
    head = [f"**updated {now:%a %m-%d %H:%M}**", "",
            f"- yesterday: **{len(posted_y)} posts** on {len({r['account'] for r in posted_y})} of {len(listed)} accounts",
            f"- last {len(week)} days with downloads: {wv:,} views → {wd} downloads"]
    snap = latest_snapshot(data)
    if snap:
        head.append(f"- App Store {snap['window_start']} .. {snap['window_end']}: {cell(snap.get('downloads'))} "
                    f"downloads · trial → paid {cell(snap.get('trial_conversion'), '{:.1%}')} · "
                    f"proceeds per install {cell(snap.get('ltv_per_install'), '${:.2f}')}")
    head.append(f"- needs a look: {', '.join(problems) if problems else 'nothing'}")
    parts = ["\n".join(head)] + ([db_tag] if db_tag else []) + [
        "## Funnel",
        "views = views so far of the posts posted that day (recent days still grow). Downloads arrive ~1 day "
        "late, impressions ~2 days: — means not in yet. per 1k views = downloads / views over the 7 days ending "
        "that day; it includes the base you get with no posts, so it is NOT what one post brings — "
        "use formats.py for that. activity = likes + comments + shares + saves, as a share of views.",
        table(["day", "posts", "views", "activity", "impressions", "downloads", "downloads per 1k views, 7d"],
              funnel_rows),
        "## Accounts",
        f"median views = posts 1-30 days old · quiet = no post for 2+ days · low views = last 3 posts under "
        f"{LOW:.0%} of that median (a shadowban sign)",
        table(["account", "label", "yesterday", "last post", "median views, 30d", "activity, 30d", "status"],
              acc_rows) if acc_rows else "no accounts listed.",
        "## Formats",
        f"score = views / that account's median, posts 3-30 days old · ~{NEED} posts per value to see a 2x gap",
        table(["format", "posts", "median score", "beat median", "activity", "readable"], fmt_rows)
        if fmt_rows else "no posts 3+ days old yet.",
    ]
    return "\n\n".join(parts) + "\n"


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0], epilog=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--no-pull", action="store_true", help="no network: rebuild from saved data")
    ap.add_argument("--days", type=int, default=14, help="funnel rows (default 14)")
    ap.add_argument("--freeze", type=int, default=30, help="days after which a funnel row is final")
    ap.add_argument("--captions", action="append", default=[], help="as in posts.py pull")
    ap.add_argument("--notion", action="store_true", help="also write to Notion (NOTION_TOKEN, NOTION_PAGE_ID)")
    ap.add_argument("--dry", action="store_true", help="with --notion: print what would change, write nothing")
    ap.add_argument("--accounts", help="accounts file (default ACCOUNTS_FILE or ./accounts.txt)")
    ap.add_argument("--data", help="data folder (default GROWTH_DATA or ./growth-data)")
    a = ap.parse_args()
    load_env()
    data = data_dir(a.data)
    failed = []
    if not a.no_pull:
        specs = a.captions or [s for s in os.environ.get("POSTS_CAPTIONS", "").split(";") if s.strip()]
        failed = P.pull(data, a.accounts, specs)
    tag = None
    if a.notion:
        import notion_sync
        tag = notion_sync.sync_posts(data, a.accounts, a.dry)
    page = build(data, failed, a.accounts, a.days, a.freeze, not a.no_pull, tag)
    out = data / "dashboard.md"
    out.write_text(page)
    print(f"wrote {out}")
    if a.notion:
        notion_sync.replace_page(page, a.dry)


if __name__ == "__main__":
    main()
