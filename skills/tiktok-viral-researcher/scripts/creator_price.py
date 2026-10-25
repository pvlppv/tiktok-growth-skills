#!/usr/bin/env python3
"""creator_price.py — what a creator's views are worth, checked against their public numbers.

    python3 creator_price.py @creator --tier1 60 --claimed 100000 --since YYYY-MM-DD --cpm CPM
    python3 creator_price.py @creator --tier1 60 --since YYYY-MM-DD --cpm CPM --cap-usd CAP
    python3 creator_price.py --config creators.json

price = paid views / 1000 x CPM, where paid views = views x tier-1 share. --cpm is your rate
(required, or "cpm" in the config). --cap-usd, if given, caps the pay per video so one freak
hit cannot eat the budget; no cap by default. tier-1 share = the % of the creator's audience
in --tier1-countries, read off their own analytics (audience by country).

Views come from --claimed (their analytics, for the deal window) or, without it, from the
public post list (yt-dlp, free) for posts made since --since. The public pull is also the
check: public views should be >= claimed, the like rate should look human, and profile likes
far above visible-post likes mean deleted or hidden posts.

--views-per-install LO-HI turns the price into a cost per install range, using your own
views-per-install figures. Off by default.

config JSON: {"cpm": CPM, "cap_usd": CAP (optional), "creators": [{"handle": "x", "tier1": 60,
              "claimed": 100000, "since": "YYYY-MM-DD", "asked_usd": 100}]}
"""
import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tt  # noqa: E402

TIER1 = "US,GB,CA,AU,NZ,IE"


def price(views_list, claimed, share, cpm, cap_usd):
    """(usd, paid_views, cut_views). The cap cut is read off public per-video views; public views
    are all-time, so for an older video the cut is an upper bound on what the window held."""
    cut = 0.0
    if cap_usd is not None:
        cap_views = cap_usd / cpm * 1000
        cut = sum(max(0.0, v * share - cap_views) for v in views_list)
    paid = (claimed if claimed is not None else sum(views_list)) * share - cut
    return max(0.0, paid) / 1000 * cpm, paid, cut


def one(c, cpm, cap_usd, vpi):
    h = tt.handle_of(c["handle"])
    entries, err = tt.ytdlp_posts(h)
    if entries is None:
        return {"handle": h, "error": err}
    since = datetime.strptime(c["since"], "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() if c.get("since") else 0
    win = [e for e in entries if (e.get("timestamp") or 0) >= since]
    views = [e.get("view_count") or 0 for e in win]
    likes = sum(e.get("like_count") or 0 for e in win)
    share = c["tier1"] / 100
    usd, paid, cut = price(views, c.get("claimed"), share, cpm, cap_usd)
    prof = tt.profile_stats(h) or {}
    vis_likes = sum(e.get("like_count") or 0 for e in entries)
    top = max(win, key=lambda e: e.get("view_count") or 0) if win else {}
    r = {"handle": h, "window_from": c.get("since") or "all", "videos": len(win), "public_views": sum(views),
         "claimed": c.get("claimed"), "tier1_pct": c["tier1"], "paid_views": round(paid), "cap_cut_views": round(cut),
         "usd": round(usd, 2), "asked_usd": c.get("asked_usd"),
         "like_pct": round(100 * likes / sum(views), 2) if sum(views) else None,
         "median_views": tt.median(views), "top_views": top.get("view_count"),
         "top_posted": time.strftime("%Y-%m-%d", time.gmtime(top.get("timestamp") or 0)) if top else None,
         "followers": prof.get("followers"), "profile_likes": prof.get("likes_total"), "visible_likes": vis_likes,
         "profile_videos": prof.get("videos"), "visible_videos": len(entries)}
    if c.get("claimed") is not None:
        r["public_covers_claim"] = sum(views) >= c["claimed"] * 0.995  # analytics rounds
    if vpi:
        lo, hi = vpi
        installs = (sum(views) if c.get("claimed") is None else c["claimed"]) / hi, \
                   (sum(views) if c.get("claimed") is None else c["claimed"]) / lo
        r["installs_est"] = [round(installs[0]), round(installs[1])]
        r["usd_per_install"] = [round(usd / installs[1], 2) if installs[1] else None,
                                round(usd / installs[0], 2) if installs[0] else None]
    return r


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("handle", nargs="?")
    ap.add_argument("--tier1", type=float, help="%% of audience in tier-1 countries")
    ap.add_argument("--claimed", type=int, help="views the creator claims for the window (their analytics)")
    ap.add_argument("--since", help="YYYY-MM-DD: only public posts from this day count")
    ap.add_argument("--asked", type=float, help="what the creator asks, USD")
    ap.add_argument("--cpm", type=float, help="USD per 1,000 tier-1 views (required unless the config sets cpm)")
    ap.add_argument("--cap-usd", type=float, help="max pay per video (default: no cap)")
    ap.add_argument("--tier1-countries", default=TIER1, help="shown in the report; the share itself is your input")
    ap.add_argument("--views-per-install", default="off", help="LO-HI range of your own views per install, or 'off' (default)")
    ap.add_argument("--config", help="JSON with several creators (see top of file)")
    ap.add_argument("--out", default="tiktok-research", help="writes creator_price.json + .md here")
    a = ap.parse_args()
    if a.config:
        cfg = json.loads(Path(a.config).read_text())
        creators, cpm, cap = cfg["creators"], cfg.get("cpm", a.cpm), cfg.get("cap_usd", a.cap_usd)
    elif a.handle and a.tier1 is not None:
        creators = [{"handle": a.handle, "tier1": a.tier1, "claimed": a.claimed, "since": a.since, "asked_usd": a.asked}]
        cpm, cap = a.cpm, a.cap_usd
    else:
        ap.error("give a handle with --tier1, or --config")
    if cpm is None:
        ap.error("--cpm is required (or set \"cpm\" in the config)")
    vpi = None if a.views_per_install == "off" else tuple(map(float, a.views_per_install.split("-")))
    rows = [one(c, cpm, cap, vpi) for c in creators]

    cap_txt = f"cap ${cap}/video ({cap / cpm * 1000:,.0f} paid views)" if cap is not None else "no cap"
    md = [f"# Creator price — CPM ${cpm}, {cap_txt}, tier-1 = {a.tier1_countries}", "",
          "| creator | window | videos | claimed | public | tier-1 | paid views | cap cut | price | asked | like % | covers claim |",
          "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        if "error" in r:
            md.append(f"| @{r['handle']} | FAILED: {r['error']} |")
            continue
        md.append(f"| @{r['handle']} | {r['window_from']} | {r['videos']} | {r['claimed'] or '-'} | {r['public_views']:,} | "
                  f"{r['tier1_pct']}% | {r['paid_views']:,} | {r['cap_cut_views']:,} | ${r['usd']:,.2f} | "
                  f"{r['asked_usd'] or '-'} | {r['like_pct']} | {r.get('public_covers_claim', '-')} |")
    md += ["", "Checks:"]
    for r in rows:
        if "error" in r:
            continue
        full = r["profile_videos"] and r["visible_videos"] >= 0.95 * r["profile_videos"]
        gap = (f"profile likes {r['profile_likes']:,} vs visible-post likes {r['visible_likes']:,} (big gap = deleted/hidden posts)"
               if full else f"only {r['visible_videos']}/{r['profile_videos'] or '?'} posts listed, so the likes gap check is off")
        md.append(f"- @{r['handle']}: top video {r['top_views'] or 0:,} ({r['top_posted']}), median {r['median_views'] or 0:,.0f}; "
                  f"followers {r['followers'] or 0:,}; {gap}.")
        if "installs_est" in r:
            md.append(f"  At {vpi[0]:.0f}-{vpi[1]:.0f} views/install: ~{r['installs_est'][0]}-{r['installs_est'][1]} installs, "
                      f"${r['usd_per_install'][0]}-{r['usd_per_install'][1]} per install (a guess, not data).")
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "creator_price.json").write_text(json.dumps(rows, indent=1))
    (out / "creator_price.md").write_text("\n".join(md) + "\n")
    print("\n".join(md))


if __name__ == "__main__":
    main()
