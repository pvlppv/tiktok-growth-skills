#!/usr/bin/env python3
"""trials.py — where free trials go: paid, cancelled (on which day), failed card, refunded.

    python3 trials.py                    # last 28 days, every product that has trials
    python3 trials.py --days 45 --product "Yearly"
    python3 trials.py --no-fetch         # cached days only, no network

Source: Apple's SUBSCRIPTION_EVENT report (SUMMARY, DAILY), one row per event type per day,
weighted by Quantity. Event names are Apple's (Start Introductory Offer, Cancel, ...).

Window caveat: these are events IN the window, not one cohort followed to the end. A cancel
or payment in the window can belong to a trial that started before it. Read the shares as a
trend; for a clean cohort, pick a window that ends at least one trial length ago.
"""

import argparse
import statistics
from collections import Counter, defaultdict

import appstore
from common import data_dir, load_env

START = "Start Introductory Offer"
# "Conversion to Standard Price" from a trial: paid, or paid on a switched plan
CONVERT = ("paid subscription from introductory offer", "crossgrade from introductory offer",
           "downgrade from introductory offer", "upgrade from introductory offer")


def qty(r):
    return int(r.get("Quantity") or 0)


def num(v):
    v = (v or "").strip()
    return int(v) if v.lstrip("-").isdigit() else None


def funnel(events, product):
    """one product's trial funnel over a list of SUBSCRIPTION_EVENT rows."""
    ev = [r for r in events if r["Subscription Name"] == product]
    count = lambda test: sum(qty(r) for r in ev if test(r["Event"].lower()))
    cancels = [r for r in ev if r["Event"] == "Cancel"]
    # Consecutive Paid Periods on a Cancel row: 0 = left before paying (in trial), 1+ = after
    in_trial = [r for r in cancels if (num(r.get("Consecutive Paid Periods")) or 0) == 0]
    after_paid = [r for r in cancels if (num(r.get("Consecutive Paid Periods")) or 0) > 0]
    days = Counter()
    for r in in_trial:
        d = num(r.get("Days Before Canceling"))
        if d is not None:
            days[d] += qty(r)
    starts = count(lambda e: e == START.lower())
    share = lambda n: round(n / starts, 3) if starts else None
    dbc = sum(days.values())
    out = {
        "product": product,
        "trial_starts": starts,
        "trial_paid": count(lambda e: e in CONVERT),
        "trial_cancelled": sum(qty(r) for r in in_trial),
        "billing_retry": count(lambda e: e.startswith("billing retry from intro")),
        "recovered_from_retry": count(lambda e: e.endswith("from billing retry") and not e.startswith("cancel")),
        "lost_in_retry": count(lambda e: e == "canceled from billing retry"),
        "refunds": count(lambda e: e.startswith("refund")),
        "cancelled_after_paying": sum(qty(r) for r in after_paid),
        "cancel_day": dict(sorted(days.items())),
        "cancel_day0_share": round(days.get(0, 0) / dbc, 3) if dbc else None,
        "cancel_median_day": statistics.median([d for d, n in days.items() for _ in range(n)]) if dbc else None,
    }
    out["trial_conversion"] = share(out["trial_paid"])
    out["trial_cancel_rate"] = share(out["trial_cancelled"])
    out["billing_retry_rate"] = share(out["billing_retry"])
    return out


def plans(events):
    """every product, trial or not: subscribe / renew / cancel, and paid periods at cancel."""
    count, periods = defaultdict(Counter), defaultdict(list)
    for r in events:
        name, e = r["Subscription Name"], r["Event"]
        if e in ("Subscribe", "Renew", "Cancel"):
            count[name][e.lower()] += qty(r)
        if e == "Cancel" and (num(r.get("Consecutive Paid Periods")) or 0) > 0:
            periods[name] += [num(r["Consecutive Paid Periods"])] * qty(r)
    return {k: {**count[k], "median_paid_periods": statistics.median(periods[k]) if periods[k] else None}
            for k in set(count) | set(periods)}


def trial_products(events):
    """products that had trial starts, most first."""
    c = Counter()
    for r in events:
        if r["Event"] == START:
            c[r["Subscription Name"]] += qty(r)
    return [p for p, _ in c.most_common()]


def show(f):
    s = f["trial_starts"]
    pct = lambda n: f"{n / s:6.1%}" if s else "     —"
    print(f"\n{f['product']} — {s} trial starts")
    for label, key in (("paid after trial", "trial_paid"), ("cancelled, never paid", "trial_cancelled"),
                       ("card failed (billing retry)", "billing_retry"),
                       ("  recovered from retry", "recovered_from_retry"),
                       ("  lost in retry", "lost_in_retry"), ("refunded", "refunds"),
                       ("cancelled after paying", "cancelled_after_paying")):
        print(f"  {label:30} {f[key]:5}  {pct(f[key])} of starts")
    if f["cancel_day"]:
        print("  days before cancelling: " + "  ".join(f"day {d}: {n}" for d, n in f["cancel_day"].items()))
        print(f"  day-0 share {f['cancel_day0_share']:.0%} · median day {f['cancel_median_day']}")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0], epilog=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=28)
    ap.add_argument("--end", help="last day, YYYY-MM-DD (default yesterday)")
    ap.add_argument("--product", help="Subscription Name to read (default: every product with trials)")
    ap.add_argument("--no-fetch", action="store_true", help="cached days only")
    ap.add_argument("--data", help="data folder (default GROWTH_DATA or ./growth-data)")
    a = ap.parse_args()
    load_env()
    dates = appstore.window(a.days, a.end)
    events = appstore.collect("SUBSCRIPTION_EVENT", dates, data_dir(a.data), not a.no_fetch)
    print(f"TRIALS · {dates[-1]} .. {dates[0]} ({a.days} days, events in window — not one cohort)")
    products = [a.product] if a.product else trial_products(events)
    if not products:
        print("no trial starts in this window")
    for p in products:
        show(funnel(events, p))
    print(f"\n{'plan':30} {'subscribe':>9} {'renew':>6} {'cancel':>6} {'median paid periods at cancel':>30}")
    for name, p in sorted(plans(events).items()):
        mp = p["median_paid_periods"]
        print(f"{name[:30]:30} {p.get('subscribe', 0):9} {p.get('renew', 0):6} {p.get('cancel', 0):6} "
              f"{mp if mp is not None else '—':>30}")


if __name__ == "__main__":
    main()
