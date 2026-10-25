# Method notes

## What each report holds (verified against Apple's docs and live headers)

`GET /v1/salesReports` with `filter[reportType]`, `filter[reportSubType]=SUMMARY`,
`filter[frequency]=DAILY`, `filter[reportDate]`, `filter[vendorNumber]`, `filter[version]`.
Answer: gzip TSV (`application/a-gzip`); 404 = no data that day.

| report | version | used for |
|---|---|---|
| SALES | 1_0 | downloads, proceeds, countries |
| SUBSCRIPTION_EVENT | 1_4 (docs say 1_3) | trials, cancels, billing retry, refunds |

SALES `Product Type Identifier`: `1` / `1F` / `1T` / `F1` first-time download (iOS, universal,
iPad, Mac) · `3`, `3F` redownload · `7`, `7F`, `7T`, `F7` update · `IAY` auto-renewable
subscription · `IA1` in-app purchase. A trial start is a `New` sale at price 0; the first real
payment shows up as a renewal, not a new sale.

SUBSCRIPTION_EVENT columns used: `Event`, `Subscription Name`, `Quantity`,
`Consecutive Paid Periods`, `Days Before Canceling`, `App Apple ID`. Event names seen live:
`Start Introductory Offer`, `Paid Subscription from Introductory Offer`, `Cancel`,
`Billing Retry from Introductory Offer` (the docs spell it "...Introductory Price"; the script
matches the prefix), `Renewal from Billing Retry`, `Canceled from Billing Retry`, `Refund`,
`Subscribe`, `Renew`.

A `Cancel` row with `Consecutive Paid Periods` = 0 is counted as "cancelled before paying";
1+ as "cancelled after paying". Reasonable reading of the column, not stated by Apple.

Impressions: analytics report "App Store Discovery and Engagement Standard", rows with
`Event = Impression`, summed `Unique Counts` per `Date`. Apple re-sends a date in up to three
later files; the newest file wins. Endpoints: `analyticsReportRequests/{id}/reports` →
`analyticsReports/{id}/instances?filter[granularity]=DAILY` →
`analyticsReportInstances/{id}/segments` → pre-signed gzip URL.

Token: ES256 JWT, header `kid` + `typ: JWT`, payload `iss`, `iat`, `exp` (≤ 20 min), `aud:
appstoreconnect-v1`.

## Two LTV definitions — do not confuse

- **Per install** = all proceeds ÷ all first-time downloads. Compare this against any cost of
  getting an install (ads, creators, paying posters). This is what `pulse.py` prints.
- **Per paying customer** = proceeds ÷ payers. How much a customer spends. Not the number for
  spend decisions.
- Link: `per install = share of installs that ever pay × per payer`.
- Rule of thumb: pay at most **per-install ÷ 3** for an install. Apple pays 33–45 days
  late, refunds happen, and new channels carry risk.
- Track monthly proceeds, not Apple's "MRR" field — it normalises yearly plans booked up front
  and reads absurdly low when most revenue is yearly.
- In a 28-day window, proceeds include renewals from installs made before it. Fine for a trend,
  slightly flattering in absolute terms for an app with many yearly renewals.

## Testing price or paywall

- Detecting a 2-point conversion gap needs thousands of installs per arm. At low volume that
  is years. Run sequential months (price A, then B) instead.
- **Compare revenue per install, never conversion rate.** A lower price always converts better
  and can still make less money.

## Post scores

- Score = views ÷ median views of the same account's posts in the same window (3–30 days old on
  the dashboard; all posts 3+ days old in Notion, so old rows stop moving).
- Activity = (likes + comments + shares + saves) ÷ views, pooled over a group (summed first, so
  a 40-view post cannot swing it).
- n per value to read a gap: the default 48 for a 2x gap assumes a spread of log score
  σ ≈ 1.2; it grows with 1 / log(gap)², so a 1.3x gap needs roughly 10x more.
- Account status on the dashboard: quiet = no post for 2+ days; low views = median of the last
  3 posts under 30% of the account's 30-day median (a shadowban sign).
- Judge a new account on its own period, not its lifetime: lifetime averages drag a recovered
  account down forever and call a two-week-old one dead.

## Downloads per format

```
downloads[d+1 .. d+B] = base + Σ b_f · views_f[d .. d+B-1] (+ c · t)
```

- B = 4 or 7 days, views per 1k (or post counts), with and without a trend: 8 variants, 90%
  intervals (statsmodels OLS). Formats with fewer than 10 posts are left out.
- `base` is per day in the output. It is everything posting does not explain: people searching
  your name, word of mouth, store features, older posts still being shown.
- Free check on where the base comes from: App Store Connect → Analytics → Sources. If most
  first-time downloads are "App Store Search" while you rank for no generic word, the base is
  brand search.
- What the regression cannot do: separate two formats that always ran together, or separate
  the parts of one format (the app card vs the list vs the topic). Only a switch can.

## Data files (in the data folder)

| file | what |
|---|---|
| `raw/SALES_<date>.tsv`, `raw/SUBSCRIPTION_EVENT_<date>.tsv` | Apple's days, cached |
| `raw/impressions/<date>_<n>.tsv` | analytics report segments |
| `snapshots/<date>.json` | one pulse each; the history |
| `posts.csv` | one row per post, rewritten each pull, whole history |
| `history.csv` | one row per young post per pull (views at a fixed age) |
| `funnel.csv` | one row per day; rows older than `--freeze` days are final |
| `dashboard.md` | the page |
| `notion.json` | ids of the Notion database |

Keep the data folder in a private backup (a private repo or a synced drive): `posts.csv` holds
matches you cannot rebuild once render folders are gone. Never commit it to a public repo.
