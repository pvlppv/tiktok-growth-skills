#!/usr/bin/env python3
"""find.py — viral TikTok photo carousels for a niche.

    python3 find.py search --query "study tips" --query "glow up" --per 25 --max-usd 1
    python3 find.py search --queries-file my_queries.txt --per 25 --max-usd 2
    python3 find.py pick                      # filter + score + slide sheets, from every raw file
    python3 find.py pick --raw OUT/raw/2026-10-24-1200.json --top 30

search (PAID): TikTok Top search through Apify's clockworks~tiktok-scraper. Top is the only
search section that returns photo carousels. Apify stops the run at --max-usd on its side.
Raw items are saved, so re-running pick is free.

pick (free): keeps carousels with 2+ slides, not ads, posted in the last --days, with
--min-views or more, and either --min-ratio x their own account's median views (read free
with yt-dlp) or --big views. A post seen in an earlier pick is skipped (seen.txt).
Writes OUT/picks/<stamp>/: hits.json, hits.md, one slide sheet per hit.
"""
import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tt  # noqa: E402

ACTOR = "clockworks~tiktok-scraper"
USD_PER_RESULT = 0.0037  # Apify free-tier price per result; check your plan
APIFY_MIN_CAP = 0.5


def search(a):
    out = Path(a.out)
    queries = list(a.query or [])
    if a.queries_file:
        queries += [l.strip() for l in Path(a.queries_file).read_text().splitlines()
                    if l.strip() and not l.startswith("#")]
    if not queries:
        sys.exit("give --query (repeatable) or --queries-file")
    est = len(queries) * a.per * USD_PER_RESULT
    print(f"{len(queries)} queries x {a.per} results ~ ${est:.2f} at ${USD_PER_RESULT}/result; cap ${a.max_usd}",
          file=sys.stderr)
    if a.max_usd < APIFY_MIN_CAP:
        sys.exit(f"Apify's smallest cap is ${APIFY_MIN_CAP}")
    if est > a.max_usd:
        print("  estimate is over the cap: Apify will stop early, the last queries may return nothing",
              file=sys.stderr)
    run_input = {"searchQueries": queries, "resultsPerPage": a.per,
                 "shouldDownloadCovers": False, "shouldDownloadSlideshowImages": False}
    items, usd = tt.apify_run(ACTOR, run_input, a.max_usd)
    (out / "raw").mkdir(parents=True, exist_ok=True)
    path = out / "raw" / f"{datetime.now():%Y-%m-%d-%H%M%S}.json"
    path.write_text(json.dumps(items, ensure_ascii=False))
    n = sum(1 for i in items if i.get("isSlideshow"))
    print(f"{len(items)} results, {n} carousels -> {path}  (${usd:.3f})")


def candidates(items, now, days, min_views, min_slides, seen=()):
    """the free filter: fresh, organic-looking photo carousels with real reach."""
    return [i for i in items
            if i.get("isSlideshow") and not i.get("isAd") and i.get("id") not in seen
            and now - (i.get("createTime") or 0) < days * tt.DAY
            and (i.get("playCount") or 0) >= min_views
            and len(i.get("slideshowImageLinks") or []) >= min_slides]  # 1 slide = a photo, not a format


def rank(cands, med, min_ratio, big, top):
    """score = views / the author's median. Keep outliers for their account (score >= min_ratio)
    or plain big hits; an unreadable account counts as exactly at the line. Rank kept ones by views."""
    for i in cands:
        m = med.get((i.get("authorMeta") or {}).get("name"))
        i["_score"] = i["playCount"] / m if m else None
    keep = [i for i in cands
            if (i["_score"] if i["_score"] is not None else min_ratio) >= min_ratio or i["playCount"] >= big]
    return sorted(keep, key=lambda i: i["playCount"], reverse=True)[:top]


def mentions_app(i):
    """a hint the post sells something: bio link, or app words in caption / bio."""
    a = i.get("authorMeta") or {}
    text = f"{i.get('text', '')} {a.get('signature', '')}".lower()
    return bool(a.get("bioLink")) or any(w in text for w in (" app", "download", "link in bio", "partner"))


def row(i, sheet):
    v = i["playCount"]
    a = i.get("authorMeta") or {}
    pct = lambda k: round(100 * (i.get(k) or 0) / v, 2) if v else None
    return {"id": i["id"], "url": i.get("webVideoUrl"), "account": a.get("name"), "followers": a.get("fans"),
            "bio": a.get("signature"), "bio_link": a.get("bioLink"), "caption": i.get("text"),
            "posted": (i.get("createTimeISO") or "")[:10], "views": v, "likes": i.get("diggCount"),
            "saves": i.get("collectCount"), "comments": i.get("commentCount"), "shares": i.get("shareCount"),
            "like_pct": pct("diggCount"), "save_pct": pct("collectCount"),
            "score": round(i["_score"], 1) if i["_score"] else None,
            "slides": len(i.get("slideshowImageLinks") or []), "sells": mentions_app(i),
            "query": i.get("searchQuery"), "sheet": sheet}


def median_views(handle):
    entries, _ = tt.ytdlp_posts(handle)
    views = [e.get("view_count") for e in entries or [] if e.get("view_count") is not None]
    return tt.median(views) if len(views) >= 5 else None


def pick(a):
    out = Path(a.out)
    raws = [Path(r) for r in a.raw] if a.raw else sorted((out / "raw").glob("*.json"))
    if not raws:
        sys.exit(f"no raw files in {out / 'raw'} (run search, or pass --raw)")
    items = list({i["id"]: i for r in raws for i in json.loads(r.read_text()) if i.get("id")}.values())
    seen_f = out / "seen.txt"
    seen = set(seen_f.read_text().split()) if seen_f.exists() and not a.reshow else set()
    cands = candidates(items, time.time(), a.days, a.min_views, a.min_slides, seen)
    authors = sorted({i["authorMeta"]["name"] for i in cands if i.get("authorMeta")})
    print(f"{len(items)} results -> {len(cands)} fresh carousels from {len(authors)} accounts", file=sys.stderr)

    med_f = Path(a.medians) if a.medians else out / "medians.json"
    med = json.loads(med_f.read_text()) if med_f.exists() else {}
    todo = [h for h in authors if h not in med]
    if todo:
        print(f"reading {len(todo)} account medians with yt-dlp (free, ~5-20 s each)", file=sys.stderr)
        with ThreadPoolExecutor(6) as ex:
            med.update(zip(todo, ex.map(median_views, todo)))
        med_f.parent.mkdir(parents=True, exist_ok=True)
        med_f.write_text(json.dumps(med, indent=0))
    hits = rank(cands, med, a.min_ratio, a.big, a.top)

    folder = out / "picks" / datetime.now().strftime("%Y-%m-%d-%H%M%S")
    folder.mkdir(parents=True, exist_ok=True)
    rows = []
    for i in hits:
        img = folder / f"{i['id']}.jpg"
        links = [s.get("downloadLink") or s.get("tiktokLink") for s in i.get("slideshowImageLinks") or []]
        if not a.no_sheets and not tt.sheet(links, img):
            continue  # slide links expire within days: re-search if every sheet fails
        rows.append(row(i, None if a.no_sheets else img.name))
    (folder / "hits.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1))
    md = ["| # | views | score | like% | save% | slides | account | posted | sells | link |",
          "|---|---|---|---|---|---|---|---|---|---|"]
    md += [f"| {n} | {r['views']:,} | {str(r['score']) + 'x' if r['score'] else '?'} | {r['like_pct']} | {r['save_pct']} | {r['slides']} | "
           f"@{r['account']} | {r['posted']} | {'yes' if r['sells'] else ''} | {r['url']} |"
           for n, r in enumerate(rows, 1)]
    (folder / "hits.md").write_text(
        f"# Viral carousels — {len(rows)} hits\n\nFilter: carousel, {a.min_slides}+ slides, not ad, last {a.days} d, "
        f"{a.min_views:,}+ views, and {a.min_ratio}x own account median or {a.big:,}+ views. "
        f"score = views / account median ('?' = account unreadable).\n\n" + "\n".join(md) + "\n")
    if not a.reshow:
        with seen_f.open("a") as f:
            f.writelines(f"{r['id']}\n" for r in rows)
    print(f"{len(rows)} hits -> {folder}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="tiktok-research", help="working folder (default ./tiktok-research)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("search", help="PAID Apify Top search")
    s.add_argument("--query", action="append", help="a search phrase; repeat the flag for more")
    s.add_argument("--queries-file", help="one phrase per line, # lines skipped")
    s.add_argument("--per", type=int, default=25, help="results per phrase (default 25)")
    s.add_argument("--max-usd", type=float, required=True, help="Apify stops at this spend (min 0.5)")
    p = sub.add_parser("pick", help="free filter + score + sheets")
    p.add_argument("--raw", nargs="*", help="raw files (default: every OUT/raw/*.json)")
    p.add_argument("--top", type=int, default=20)
    p.add_argument("--days", type=int, default=180, help="freshness window (Top search leans old)")
    p.add_argument("--min-views", type=int, default=20_000, help="3x a 300-view account is noise")
    p.add_argument("--min-ratio", type=float, default=3.0, help="views / own account median")
    p.add_argument("--big", type=int, default=500_000, help="kept whatever the ratio")
    p.add_argument("--min-slides", type=int, default=2)
    p.add_argument("--medians", help="JSON {handle: median} cache (default OUT/medians.json)")
    p.add_argument("--no-sheets", action="store_true", help="skip slide downloads")
    p.add_argument("--reshow", action="store_true", help="ignore and do not update seen.txt")
    a = ap.parse_args()
    {"search": search, "pick": pick}[a.cmd](a)


if __name__ == "__main__":
    main()
