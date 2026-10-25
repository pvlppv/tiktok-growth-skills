#!/usr/bin/env python3
"""pulse.py — what moved: a rolling App Store window, saved as a snapshot, diffed against the last one.

    python3 pulse.py                     # last 28 days, saved + diffed
    python3 pulse.py --days 14
    python3 pulse.py --end 2026-01-31    # backfill an older window (snapshot stamped with that date)
    python3 pulse.py --no-fetch --no-save
    python3 pulse.py --tiktok            # add follower / like / post totals of the accounts file

Raw days are cached in <data>/raw/, so a re-run only downloads what is missing.
Snapshots: <data>/snapshots/YYYY-MM-DD.json. The baseline is the newest snapshot from an
EARLIER day, so a second run on the same day still compares against the last real one.
"""

import argparse
import csv
import datetime as dt
import json
import re
import urllib.request
from collections import Counter
from pathlib import Path

import appstore
import trials
from common import accounts, data_dir, load_env

# Proceeds arrive in the currency of proceeds. Rates are hand-set and stamped; an unknown
# currency is counted 1:1 and printed as a warning. Override with --fx rates.json.
FX_DATE = "2026-08-25"
FX = {"USD": 1, "GBP": 1.27, "EUR": 1.08, "DKK": 0.145, "AUD": 0.66, "NOK": 0.093, "SEK": 0.094,
      "IDR": 0.0000615, "CLP": 0.00105, "CNY": 0.14, "PLN": 0.25, "CAD": 0.73, "JPY": 0.0067,
      "CHF": 1.13, "MXN": 0.050, "BRL": 0.18, "INR": 0.0115, "TRY": 0.026, "RUB": 0.0114,
      "NZD": 0.60, "ZAR": 0.055, "KRW": 0.00072, "THB": 0.029, "CZK": 0.043, "HUF": 0.0028,
      "RON": 0.22, "ILS": 0.27, "SGD": 0.77, "HKD": 0.128, "TWD": 0.031, "PHP": 0.017,
      "MYR": 0.22, "VND": 0.000038, "AED": 0.27, "SAR": 0.27, "COP": 0.00025, "PEN": 0.27,
      "ARS": 0.00070, "UAH": 0.024, "EGP": 0.021, "NGN": 0.00065, "PKR": 0.0036, "BGN": 0.55,
      "KZT": 0.0019, "QAR": 0.27, "TZS": 0.00037}

# key, label, format, good direction (+1 more is good, -1 more is bad, 0 neutral)
FIELDS = [
    ("downloads", "first-time downloads", "{:.0f}", 1),
    ("proceeds_usd", "proceeds $", "${:.0f}", 1),
    ("ltv_per_install", "proceeds per install", "${:.2f}", 1),
    ("trial_starts", "trial starts", "{:.0f}", 1),
    ("install_to_trial", "install -> trial", "{:.1%}", 1),
    ("trial_paid", "paid after trial", "{:.0f}", 1),
    ("trial_conversion", "trial -> paid", "{:.1%}", 1),
    ("trial_cancelled", "trials cancelled", "{:.0f}", 0),
    ("trial_cancel_rate", "trial cancel rate", "{:.1%}", -1),
    ("cancel_day0_share", "  of them on day 0", "{:.1%}", -1),
    ("billing_retry", "card failed", "{:.0f}", -1),
    ("refunds", "refunds", "{:.0f}", -1),
    ("cancelled_after_paying", "cancelled after paying", "{:.0f}", -1),
    ("top_period_share", "top period's revenue share", "{:.1%}", 0),
]


def build(days, data, fetch=True, end=None, fx=FX):
    dates = appstore.window(days, end)
    sales = appstore.collect("SALES", dates, data, fetch)
    events = appstore.collect("SUBSCRIPTION_EVENT", dates, data, fetch)
    m = {"window_days": days, "window_start": dates[-1], "window_end": dates[0],
         "generated": end or dt.date.today().isoformat(), "fx_date": FX_DATE}
    units, by_period, countries, unknown = Counter(), Counter(), Counter(), set()
    proceeds = 0.0
    for r in sales:
        q, t = int(r["Units"] or 0), r["Product Type Identifier"]
        units[t] += q
        ccy = r.get("Currency of Proceeds") or "USD"
        if float(r["Developer Proceeds"] or 0) and ccy not in fx:
            unknown.add(ccy)
        # Developer Proceeds is PER UNIT and in local currency: multiply, then convert
        v = float(r["Developer Proceeds"] or 0) * q * fx.get(ccy, 1.0)
        proceeds += v
        if v:
            by_period[r.get("Period", "").strip() or "one-off"] += v
        if t in appstore.DOWNLOAD:
            countries[r["Country Code"]] += q
    dl = sum(units[t] for t in appstore.DOWNLOAD)
    m.update(downloads=dl, redownloads=sum(units[t] for t in appstore.REDOWNLOAD),
             updates=sum(units[t] for t in appstore.UPDATE), proceeds_usd=round(proceeds, 2),
             ltv_per_install=round(proceeds / dl, 2) if dl else None,
             revenue_by_period={k: round(v, 2) for k, v in by_period.most_common()},
             top_period_share=round(by_period.most_common(1)[0][1] / proceeds, 3) if proceeds and by_period else None,
             top_countries=countries.most_common(8), unknown_currencies=sorted(unknown))
    products = trials.trial_products(events)
    if products:   # the product with the most trial starts carries the headline trial numbers
        f = trials.funnel(events, products[0])
        m["trial_product"] = products[0]
        m.update({k: f[k] for k in ("trial_starts", "trial_paid", "trial_conversion", "trial_cancelled",
                                    "trial_cancel_rate", "cancel_day0_share", "billing_retry",
                                    "cancelled_after_paying")})
        m["install_to_trial"] = round(f["trial_starts"] / dl, 3) if dl else None
    m["refunds"] = sum(trials.qty(r) for r in events if r["Event"].lower().startswith("refund"))
    m["plans"] = trials.plans(events)
    return m


UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def profile(handle):
    """exact follower / like / post totals from the public profile page (statsV2), no key."""
    req = urllib.request.Request(f"https://www.tiktok.com/@{handle}", headers={"User-Agent": UA})
    html = urllib.request.urlopen(req, timeout=20).read().decode("utf-8", "ignore")
    blob = re.search(r"__UNIVERSAL_DATA_FOR_REHYDRATION__[^>]*>(.*?)</script>", html, re.S)
    s = json.loads(blob.group(1))["__DEFAULT_SCOPE__"]["webapp.user-detail"]["userInfo"]["statsV2"]
    return {k: int(s[k]) for k in ("followerCount", "heartCount", "videoCount")}


def tiktok(path=None):
    out = {}
    for h, _, _ in accounts(path):
        try:
            out[h] = profile(h)
        except Exception as e:  # private, renamed, blocked: say so, never stop the pulse
            out[h] = {"error": str(e)[:80]}
    return out


def fmt(v, f):
    return "—" if v is None else f.format(v)


def diff(cur, prev, data):
    print(f"\nPULSE · {cur['window_start']} .. {cur['window_end']} ({cur['window_days']} days)")
    print(f"vs {prev['window_start']} .. {prev['window_end']}\n" if prev else "first snapshot — nothing to compare\n")
    print(f"{'':28} {'now':>10} {'before':>10} {'delta':>10}")
    for key, label, f, good in FIELDS:
        c, p = cur.get(key), (prev or {}).get(key)
        if c is None and p is None:
            continue
        delta = ""
        if c is not None and p not in (None, 0):
            pct = (c - p) / abs(p)
            mark = "" if abs(pct) < 0.05 or not good else (" +" if pct * good > 0 else " -")
            delta = f"{pct:+.0%}{mark}"
        print(f"{label:28} {fmt(c, f):>10} {fmt(p, f):>10} {delta:>10}")
    print("  + / - = moved 5%+ in the good / bad direction for that metric")
    if cur.get("trial_product"):
        print(f"  trial numbers are for '{cur['trial_product']}', the product with most trial starts")
    tk, tp = cur.get("tiktok"), (prev or {}).get("tiktok") or {}
    if tk:
        print(f"\n{'account':26} {'posts':>6} {'likes':>9} {'followers':>9} {'+posts':>7} {'+likes':>8} {'likes/post':>10}")
        dp = dl = 0
        for h, v in tk.items():
            if "error" in v:
                print(f"{h:26} ERROR {v['error']}")
                continue
            o = tp.get(h)
            o = o if o and "error" not in o else None
            p_, l_ = (v["videoCount"] - o["videoCount"], v["heartCount"] - o["heartCount"]) if o else (None, None)
            dp, dl = dp + (p_ or 0), dl + (l_ or 0)
            # judge on the period once there is a baseline: lifetime averages condemn new accounts
            lpp = l_ / p_ if p_ else (v["heartCount"] / v["videoCount"] if v["videoCount"] else 0)
            print(f"{h:26} {v['videoCount']:6} {v['heartCount']:9,} {v['followerCount']:9,} "
                  f"{'' if p_ is None else f'{p_:+}':>7} {'' if l_ is None else f'{l_:+,}':>8} {lpp:10.0f}")
        if dp > 0:
            print(f"\n  between snapshots: {dp} posts, {dl:,} likes, {cur['downloads'] / dp:.2f} downloads per post"
                  " (all downloads, incl. the base you get without posting)")
        ratio = views_per_like(data)
        if ratio and dl:
            print(f"  views estimate: {dl * ratio:,.0f} ({ratio:.0f} views per like, from your posts.csv)")
    if cur.get("unknown_currencies"):
        print(f"\n  WARNING: unknown currency {cur['unknown_currencies']} counted 1:1 — add a rate with --fx")
    print(f"\n  FX rates fixed at {cur['fx_date']} · proceeds = net of Apple's commission")


def views_per_like(data):
    """your own views/likes ratio from posts.csv, or None. It varies a lot between
    accounts, so it is a trend line, never a payout basis."""
    p = Path(data) / "posts.csv"
    if not p.exists():
        return None
    rs = list(csv.DictReader(p.open()))
    likes = sum(int(r["likes"] or 0) for r in rs)
    return sum(int(r["views"] or 0) for r in rs) / likes if likes else None


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0], epilog=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=28)
    ap.add_argument("--end", help="last day of the window, YYYY-MM-DD (default yesterday)")
    ap.add_argument("--no-fetch", action="store_true", help="cached days only")
    ap.add_argument("--no-save", action="store_true", help="print, write no snapshot")
    ap.add_argument("--tiktok", action="store_true", help="add profile totals of the accounts file")
    ap.add_argument("--accounts", help="accounts file (default ACCOUNTS_FILE or ./accounts.txt)")
    ap.add_argument("--fx", help="JSON {currency: USD per unit} merged over the built-in rates")
    ap.add_argument("--data", help="data folder (default GROWTH_DATA or ./growth-data)")
    a = ap.parse_args()
    load_env()
    data = data_dir(a.data)
    fx = {**FX, **(json.loads(Path(a.fx).read_text()) if a.fx else {})}
    cur = build(a.days, data, not a.no_fetch, a.end, fx)
    if a.tiktok:
        cur["tiktok"] = tiktok(a.accounts)
    snaps = data / "snapshots"
    snaps.mkdir(exist_ok=True)
    older = [f for f in sorted(snaps.glob("[0-9]*.json")) if f.stem < cur["generated"]]
    diff(cur, json.loads(older[-1].read_text()) if older else None, data)
    if not a.no_save:
        p = snaps / f"{cur['generated']}.json"
        p.write_text(json.dumps(cur, indent=2, ensure_ascii=False))
        print(f"  snapshot saved: {p}")


if __name__ == "__main__":
    main()
