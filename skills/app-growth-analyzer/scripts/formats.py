#!/usr/bin/env python3
"""formats.py — which post format actually adds App Store downloads.

    python3 formats.py                                   # every format in posts.csv, all shared days
    python3 formats.py --start 2026-01-01 --end 2026-03-31 --formats tips stories
    python3 formats.py --window "before=2026-01-01..2026-01-14" --window "tips on=2026-01-15..2026-02-10"
    python3 formats.py --downloads downloads.csv         # day,downloads instead of the ASC cache

TikTok does not pass the install source on, so there is no per-post install number. This
regresses DAILY first-time downloads (ASC SALES cache) on the views (or post count) of each
format's posts, by the day they were posted, with downloads lagged 1 day:

    downloads[bucket+1d] = base + b1 * views_format1 + b2 * views_format2 + ... (+ trend)

The constant is the base you get without posting (mostly people searching your app's name).
Each b is "extra downloads per 1k views" (or per post) of that format. It prints 8 variants
(per 1k views / per post, 4- and 7-day buckets, with and without a time trend) with 90%
intervals. Trust a format only when its interval stays on one side of 0 in most variants.

--window prints downloads/day and views/day per format for date ranges you name: switching a
format on or off is the only clean experiment you have; the regression is the second-best.
"""

import argparse
import csv
import datetime as dt
from collections import Counter, defaultdict
from pathlib import Path

import appstore
import posts as P
from common import data_dir, load_env

MIN_POSTS = 10    # a format with fewer posts only adds a noise column


def daily(posts, formats):
    views, count = defaultdict(Counter), defaultdict(Counter)
    for r in posts:
        f = P.fmt(r["carousel"])
        if f in formats:
            views[r["posted"][:10]][f] += int(r["views"] or 0)
            count[r["posted"][:10]][f] += 1
    return views, count


def downloads(data, path):
    if path:
        return {r["day"]: int(r["downloads"]) for r in csv.DictReader(Path(path).open())}
    return appstore.downloads_by_day(data)


def model(views, count, dls, formats, start, end, bucket, kind, trend, lag=1):
    import numpy as np
    import statsmodels.api as sm
    X, Y, T = [], [], []
    d = start
    while d + dt.timedelta(bucket + lag - 1) <= end:
        days = [(d + dt.timedelta(i)).isoformat() for i in range(bucket)]
        later = [(d + dt.timedelta(i + lag)).isoformat() for i in range(bucket)]
        if all(k in dls for k in later):
            src, div = (views, 1000) if kind == "views" else (count, 1)
            X.append([sum(src[k][f] for k in days) / div for f in formats])
            Y.append(sum(dls[k] for k in later))
            T.append((d - start).days)
        d += dt.timedelta(bucket)
    if len(Y) < len(formats) + 3:
        return None, len(Y)
    Z = np.column_stack([X, T]) if trend else np.array(X, dtype=float)
    return sm.OLS(np.array(Y, dtype=float), sm.add_constant(Z, has_constant="add")).fit(), len(Y)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0], epilog=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--formats", nargs="*", help="formats to model (default: every one in posts.csv)")
    ap.add_argument("--start", help="first day, YYYY-MM-DD (default: first day with both data)")
    ap.add_argument("--end", help="last day (default: last day with both data)")
    ap.add_argument("--lag", type=int, default=1, help="days from post to download (default 1)")
    ap.add_argument("--window", action="append", default=[], help="'label=YYYY-MM-DD..YYYY-MM-DD' (repeatable)")
    ap.add_argument("--downloads", help="CSV with day,downloads instead of the ASC cache")
    ap.add_argument("--data", help="data folder (default GROWTH_DATA or ./growth-data)")
    a = ap.parse_args()
    load_env()
    data = data_dir(a.data)
    rows = list(P.load(data).values())
    dls = downloads(data, a.downloads)
    if not rows or not dls:
        raise SystemExit("need posts.csv (posts.py pull) and downloads (pulse.py / appstore.py fetch)")
    n = Counter(P.fmt(r["carousel"]) for r in rows)
    formats = a.formats or sorted(f for f, k in n.items() if k >= MIN_POSTS)
    skipped = sorted(set(n) - set(formats))
    if skipped:
        print(f"left out (fewer than {MIN_POSTS} posts or not asked for): {', '.join(skipped)}")
    views, count = daily(rows, formats)
    first = max(min(views), min(dls))
    last = min(max(views), max(dls))
    start = dt.date.fromisoformat(a.start or first)
    end = dt.date.fromisoformat(a.end or last)
    print(f"## downloads per format, {start} .. {end}, lag {a.lag}d, 90% intervals")
    print("   base/day = downloads you get with zero posts (people searching your name, word of mouth, ...)")
    for kind in ("views", "posts"):
        unit = "per 1k views" if kind == "views" else "per post"
        for trend in (False, True):
            for bucket in (4, 7):
                m, n = model(views, count, dls, formats, start, end, bucket, kind, trend, a.lag)
                head = f"{unit:12} {bucket}d trend={'yes' if trend else 'no ':3} n={n:3}"
                if m is None:
                    print(f"{head} | too few buckets")
                    continue
                ci = m.conf_int(0.10)
                cells = [f"{f} {m.params[i + 1]:+.3f} [{ci[i + 1][0]:+.3f},{ci[i + 1][1]:+.3f}]"
                         for i, f in enumerate(formats)]
                print(f"{head} | base/day {m.params[0] / bucket:5.1f} | " + " | ".join(cells))
    if a.window:
        print("\n## on / off windows")
    for w in a.window:
        label, _, span = w.partition("=")
        lo, _, hi = span.partition("..")
        d, n, dsum, v = dt.date.fromisoformat(lo), 0, 0, Counter()
        while d <= dt.date.fromisoformat(hi):
            k = d.isoformat()
            dsum, n = dsum + dls.get(k, 0), n + 1
            v.update(views[k])
            d += dt.timedelta(1)
        print(f"{label:16} {lo}..{hi} | downloads/day {dsum / n:6.1f} | views/day "
              + "  ".join(f"{f} {v[f] / n:,.0f}" for f in formats))
    print("\ncaveats: a format that ran alongside another cannot be separated from it; few buckets = wide"
          " intervals;\n  a views->downloads % on a dashboard is base / views, not what one post brings.")


if __name__ == "__main__":
    main()
