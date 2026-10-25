#!/usr/bin/env python3
"""appstore.py — App Store Connect reports, cached one day per file.

    python3 appstore.py fetch --days 28        # fill the cache (SALES + SUBSCRIPTION_EVENT)
    python3 appstore.py fetch --impressions    # also the daily analytics impressions report
    python3 appstore.py day SALES 2026-01-15   # print one cached/fetched day as TSV

Keys from env / .env: ASC_KEY_ID, ASC_ISSUER_ID, ASC_P8 (path to the .p8 file),
ASC_VENDOR_NUMBER. Optional: ASC_APP_ID (filter subscription events to one app),
ASC_APP_SKU (filter sales rows to one app), ASC_ANALYTICS_REQUEST_ID (impressions).
The key needs the Sales or Admin role for sales; analytics needs Admin.

Only GET requests. Nothing here spends money or writes to App Store Connect.
"""

import argparse
import csv
import datetime as dt
import gzip
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from pathlib import Path

from common import data_dir, load_env, need

API = "https://api.appstoreconnect.apple.com"
# Apple's docs table lists SUBSCRIPTION_EVENT as 1_3; 1_4 is what live accounts return today.
# Override with ASC_EVENT_VERSION if Apple answers 400 for your account.
VERSION = {"SALES": "1_0", "SUBSCRIPTION_EVENT": "1_4"}
DOWNLOAD = {"1", "1F", "1T", "F1"}      # first-time downloads (iOS, universal, iPad, Mac)
REDOWNLOAD = {"3", "3F"}
UPDATE = {"7", "7F", "7T", "F7"}
IMPRESSIONS_REPORT = "App Store Discovery and Engagement Standard"


def token():
    import jwt  # PyJWT[crypto]
    need("ASC_KEY_ID", "ASC_ISSUER_ID", "ASC_P8")
    now = int(time.time())
    key = Path(os.environ["ASC_P8"]).expanduser().read_text()
    return jwt.encode({"iss": os.environ["ASC_ISSUER_ID"], "iat": now, "exp": now + 1200,
                       "aud": "appstoreconnect-v1"}, key, algorithm="ES256",
                      headers={"kid": os.environ["ASC_KEY_ID"], "typ": "JWT"})


def get(url, accept="application/json"):
    """(status, bytes). 404 is a normal answer: Apple's "no data for that day"."""
    req = urllib.request.Request(url if url.startswith("http") else API + url,
                                 headers={"Authorization": f"Bearer {token()}", "Accept": accept})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def unzip(raw):
    try:
        return gzip.decompress(raw).decode("utf-8")
    except OSError:
        return raw.decode("utf-8")


def report(kind, date):
    """one SUMMARY DAILY report as TSV text, '' when Apple has nothing for that day."""
    need("ASC_VENDOR_NUMBER")
    q = {"filter[reportType]": kind, "filter[reportSubType]": "SUMMARY", "filter[frequency]": "DAILY",
         "filter[reportDate]": date, "filter[vendorNumber]": os.environ["ASC_VENDOR_NUMBER"],
         "filter[version]": (kind != "SALES" and os.environ.get("ASC_EVENT_VERSION")) or VERSION[kind]}
    status, body = get("/v1/salesReports?" + urllib.parse.urlencode(q), "application/a-gzip")
    if status == 200:
        return unzip(body)
    if status == 404:
        return ""
    sys.exit(f"App Store Connect {kind} {date}: HTTP {status} {body[:300].decode('utf-8', 'ignore')}")


def day(kind, date, data, fetch=True):
    """cached TSV of one day. A day is cached once Apple has data for it, or once it is 3+ days
    old — never an empty recent day, or it would freeze as empty forever."""
    path = data / "raw" / f"{kind}_{date}.tsv"
    if path.exists():
        return path.read_text()
    if not fetch:
        return ""
    text = report(kind, date)
    if text.strip() or date < (dt.date.today() - dt.timedelta(days=3)).isoformat():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return text


def rows(text):
    return list(csv.DictReader(text.splitlines(), delimiter="\t")) if text.strip() else []


def mine(kind, rs):
    """keep one app's rows when the vendor account holds several."""
    app, sku = os.environ.get("ASC_APP_ID"), os.environ.get("ASC_APP_SKU")
    if kind == "SUBSCRIPTION_EVENT" and app:
        return [r for r in rs if r.get("App Apple ID") == app]
    if kind == "SALES" and sku:
        return [r for r in rs if sku in (r.get("SKU"), r.get("Parent Identifier"))]
    return rs


def window(days, end=None):
    """the window's dates, newest first. Default end = yesterday (Apple closes a day late)."""
    last = dt.date.fromisoformat(end) if end else dt.date.today() - dt.timedelta(days=1)
    return [(last - dt.timedelta(days=i)).isoformat() for i in range(days)]


def collect(kind, dates, data, fetch=True):
    out = []
    for d in dates:
        out += mine(kind, rows(day(kind, d, data, fetch)))
    return out


def downloads_by_day(data):
    """{date: first-time downloads} from every cached SALES day."""
    out = {}
    for f in sorted((data / "raw").glob("SALES_*.tsv")):
        out[f.stem[6:]] = sum(int(r["Units"] or 0) for r in mine("SALES", rows(f.read_text()))
                              if r["Product Type Identifier"] in DOWNLOAD)
    return out


# ---- analytics reports (impressions) -----------------------------------------------------------

def fetch_impressions(data, keep=40):
    """download the newest daily instances of the impressions report into raw/impressions/.
    Needs ASC_ANALYTICS_REQUEST_ID: an ONGOING report request (create it once in the API).
    Apple builds instances 24-48 h after the request is made; none yet is not an error."""
    rid = os.environ.get("ASC_ANALYTICS_REQUEST_ID")
    if not rid:
        return False
    out = data / "raw" / "impressions"
    out.mkdir(parents=True, exist_ok=True)
    js = lambda path: json.loads(get(path)[1] or b"{}").get("data") or []
    q = urllib.parse.urlencode({"filter[name]": IMPRESSIONS_REPORT, "limit": 200})
    rep = js(f"/v1/analyticsReportRequests/{rid}/reports?{q}")
    if not rep:
        print(f"no '{IMPRESSIONS_REPORT}' report under that request yet")
        return False
    inst = js(f"/v1/analyticsReports/{rep[0]['id']}/instances?filter[granularity]=DAILY&limit=200")
    for i in sorted(inst, key=lambda i: i["attributes"]["processingDate"])[-keep:]:
        date = i["attributes"]["processingDate"]
        if list(out.glob(f"{date}_*.tsv")):
            continue                      # a processed day never changes
        for n, s in enumerate(js(f"/v1/analyticsReportInstances/{i['id']}/segments")):
            with urllib.request.urlopen(s["attributes"]["url"], timeout=120) as r:   # pre-signed URL
                (out / f"{date}_{n}.tsv").write_text(unzip(r.read()))
    return True


def impressions_by_day(data):
    """{date: unique impressions}. Apple re-sends a date in later files: the newest file wins."""
    out = {}
    for f in sorted((data / "raw" / "impressions").glob("*.tsv")):
        per = defaultdict(int)
        for r in rows(f.read_text()):
            if r.get("Event") == "Impression" and (not os.environ.get("ASC_APP_ID")
                                                   or r.get("App Apple Identifier") == os.environ["ASC_APP_ID"]):
                per[r["Date"]] += int(r.get("Unique Counts") or 0)
        out.update(per)
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0], epilog=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", help="data folder (default GROWTH_DATA or ./growth-data)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch", help="fill the cache for a window")
    f.add_argument("--days", type=int, default=28)
    f.add_argument("--end", help="last day, YYYY-MM-DD (default yesterday)")
    f.add_argument("--impressions", action="store_true", help="also pull the impressions report")
    d = sub.add_parser("day", help="print one day's report")
    d.add_argument("kind", choices=sorted(VERSION))
    d.add_argument("date")
    a = ap.parse_args()
    load_env()
    data = data_dir(a.data)
    if a.cmd == "day":
        print(day(a.kind, a.date, data), end="")
        return
    dates = window(a.days, a.end)
    for kind in VERSION:
        n = len(collect(kind, dates, data))
        print(f"{kind:20} {len(dates)} days, {n} rows")
    if a.impressions:
        print("impressions:", "ok" if fetch_impressions(data) else "skipped (no ASC_ANALYTICS_REQUEST_ID)")


if __name__ == "__main__":
    main()
