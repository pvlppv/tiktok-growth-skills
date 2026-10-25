#!/usr/bin/env python3
"""notion_sync.py — posts.csv as a sortable Notion database, and the dashboard as a Notion page.

    python3 notion_sync.py posts --dry             # say what would change, write nothing
    python3 notion_sync.py posts                   # create "All posts" once, then add / update rows
    python3 notion_sync.py page growth-data/dashboard.md

Needs NOTION_TOKEN (an internal integration) and NOTION_PAGE_ID (the page that holds it all).
Share that page with the integration first (page ••• → Connections), or every call is a 404.

Rows are only added or updated, never deleted: a hidden or deleted TikTok keeps its last
numbers, the same as in posts.csv. The database is a NON-inline child of the page; the page
replace names it with a <database> tag, so Notion keeps it (replace refuses to delete a child
database, and this script never passes allow_deleting_content).
Ids live in <data>/notion.json so every run finds the same database again.
"""

import argparse
import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict
from pathlib import Path

import posts as P
from common import accounts, data_dir, load_env, need

API = "https://api.notion.com/v1"
VERSION = "2026-03-11"
PROPS = {
    "Post": {"title": {}},
    "Posted": {"date": {}},
    "Account": {"select": {}},
    "Label": {"select": {}},
    "Format": {"select": {}},
    "Views": {"number": {"format": "number_with_commas"}},
    "Likes": {"number": {"format": "number_with_commas"}},
    "Comments": {"number": {"format": "number_with_commas"}},
    "Shares": {"number": {"format": "number_with_commas"}},
    "Saves": {"number": {"format": "number_with_commas"}},
    "Activity": {"number": {"format": "percent"}},
    "Score": {"number": {"format": "number"}},
    "Link": {"url": {}},
    "ID": {"rich_text": {}},
}
NUMBERS = ("Views", "Likes", "Comments", "Shares", "Saves", "Activity", "Score")
SELECTS = ("Format", "Label")       # compared too, so a column added later fills itself


def call(method, path, body=None):
    """one Notion call; waits out 429 (Retry-After), retries 5xx and dropped links."""
    for attempt in range(6):
        req = urllib.request.Request(API + path, method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Authorization": f"Bearer {os.environ['NOTION_TOKEN']}",
                                              "Notion-Version": VERSION, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(float(e.headers.get("Retry-After", 5)))
                continue
            if e.code < 500:
                sys.exit(f"Notion {method} {path} {e.code}: {e.read()[:300].decode('utf-8', 'ignore')}")
        except urllib.error.URLError:
            pass
        time.sleep(2 ** attempt)
    sys.exit(f"Notion {method} {path} failed after 6 tries")


def ids_file(data):
    return Path(data) / "notion.json"


def source(data):
    """the data source id of "All posts", creating the database the first time."""
    f = ids_file(data)
    if f.exists():
        return json.loads(f.read_text())["data_source"]
    db = call("POST", "/databases", {
        "parent": {"type": "page_id", "page_id": os.environ["NOTION_PAGE_ID"]},
        "title": [{"type": "text", "text": {"content": "All posts"}}],
        "is_inline": False,
        "initial_data_source": {"properties": PROPS},
    })
    ids = {"database": db["id"], "data_source": db["data_sources"][0]["id"], "url": db["url"]}
    f.write_text(json.dumps(ids, indent=1) + "\n")
    print(f"created All posts: {db['url']}")
    return ids["data_source"]


def db_tag(data):
    """the <database> tag the page must carry so a full replace keeps the database."""
    f = ids_file(data)
    if not f.exists():
        return None
    j = json.loads(f.read_text())
    return f'<database url="{j["url"]}" inline="false" data-source-url="collection://{j["data_source"]}">All posts</database>'


def existing(ds):
    """{post id: (notion page id, {column: value})} for every row already there."""
    rows, cursor = {}, None
    while True:
        res = call("POST", f"/data_sources/{ds}/query", {"page_size": 100, **({"start_cursor": cursor} if cursor else {})})
        for p in res["results"]:
            pr = p["properties"]
            pid = "".join(t["plain_text"] for t in pr["ID"]["rich_text"])
            rows[pid] = (p["id"], {**{k: (pr.get(k) or {}).get("number") for k in NUMBERS},
                                   **{k: ((pr.get(k) or {}).get("select") or {}).get("name") for k in SELECTS}})
        if not res.get("has_more"):
            return rows
        cursor = res["next_cursor"]


def wanted(data, accounts_file=None):
    """{post id: Notion properties}. Score = views / the account's ALL-TIME median of posts 3+ days
    old — a stable yardstick, where the dashboard's 3-30 day window would rewrite old rows daily."""
    rows = list(P.load(data).values())
    label = {h: l for h, l, _ in accounts(accounts_file)}
    old = defaultdict(list)
    for r in rows:
        if P.age_days(r) >= 3:
            old[r["account"]].append(int(r["views"] or 0))
    med = {a: statistics.median(v) for a, v in old.items()}
    text = lambda s: [{"text": {"content": s}}] if s else []
    out = {}
    for r in rows:
        views = int(r["views"] or 0)
        out[r["id"]] = {
            "Post": {"title": text(r["carousel"].split("/")[-1] if r["carousel"] else r["id"])},
            "Posted": {"date": {"start": r["posted"].replace(" ", "T") + ":00Z"}},
            "Account": {"select": {"name": r["account"]}},
            "Label": {"select": {"name": label.get(r["account"]) or "—"}},
            "Format": {"select": {"name": P.fmt(r["carousel"])}},
            "Views": {"number": views},
            **{k.capitalize(): {"number": int(r[k] or 0)} for k in ("likes", "comments", "shares", "saves")},
            "Activity": {"number": round(P.activity([r]), 4) if views else None},
            "Score": {"number": round(views / med[r["account"]], 2) if med.get(r["account"]) else None},
            "Link": {"url": r.get("url") or None},
            "ID": {"rich_text": text(r["id"])},
        }
    return out


def sync_posts(data, accounts_file=None, dry=False):
    """add new posts, update changed numbers. Returns the database tag for the page."""
    need("NOTION_TOKEN", "NOTION_PAGE_ID")
    want = wanted(data, accounts_file)
    if dry and not ids_file(data).exists():
        print(f"notion: would create All posts and add {len(want)} rows")
        return None
    ds = source(data)
    if not dry:   # add any column the database lacks; existing columns and rows are left alone
        have_cols = call("GET", f"/data_sources/{ds}")["properties"]
        missing = {k: v for k, v in PROPS.items() if k not in have_cols}
        if missing:
            call("PATCH", f"/data_sources/{ds}", {"properties": missing})
    have = existing(ds)
    value = lambda k, c: want[k][c]["number"] if c in NUMBERS else want[k][c]["select"]["name"]
    cols = NUMBERS + SELECTS
    add = [k for k in want if k not in have]
    change = [k for k in want if k in have and any(have[k][1].get(c) != value(k, c) for c in cols)]
    print(f"notion: {len(want)} posts — {len(add)} new, {len(change)} changed, "
          f"{len(want) - len(add) - len(change)} unchanged{' (dry run)' if dry else ''}")
    if not dry:
        for n, k in enumerate(add, 1):
            call("POST", "/pages", {"parent": {"type": "data_source_id", "data_source_id": ds}, "properties": want[k]})
            time.sleep(0.34)          # Notion's limit is ~3 requests a second
            if n % 200 == 0:
                print(f"  added {n}/{len(add)}")
        for k in change:
            call("PATCH", f"/pages/{have[k][0]}", {"properties": {c: want[k][c] for c in cols}})
            time.sleep(0.34)
    return db_tag(data)


def replace_page(markdown, dry=False):
    """replace NOTION_PAGE_ID's whole content with the dashboard markdown."""
    need("NOTION_TOKEN", "NOTION_PAGE_ID")
    if dry:
        print(f"notion: would replace the page with {len(markdown):,} characters of markdown")
        return
    call("PATCH", f"/pages/{os.environ['NOTION_PAGE_ID']}/markdown",
         {"type": "replace_content", "replace_content": {"new_str": markdown}})
    print("notion: page replaced")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0], epilog=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", help="data folder (default GROWTH_DATA or ./growth-data)")
    ap.add_argument("--accounts", help="accounts file (default ACCOUNTS_FILE or ./accounts.txt)")
    ap.add_argument("--dry", action="store_true", help="say what would change, write nothing")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("posts", help="sync posts.csv into the All posts database")
    pg = sub.add_parser("page", help="replace the Notion page with a markdown file")
    pg.add_argument("file")
    a = ap.parse_args()
    load_env()
    data = data_dir(a.data)
    if a.cmd == "posts":
        sync_posts(data, a.accounts, a.dry)
    else:
        text = Path(a.file).read_text()
        tag = db_tag(data)
        replace_page(text if not tag or tag in text else tag + "\n\n" + text, a.dry)


if __name__ == "__main__":
    main()
