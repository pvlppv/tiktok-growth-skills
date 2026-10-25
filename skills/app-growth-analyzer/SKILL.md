---
name: app-growth-analyzer
description: Reads an iOS app's App Store Connect sales and subscription reports plus the public stats of your own TikTok accounts, and returns what moved in the last 28 days, where free trials cancel (which day, failed cards, cancels after paying), every post's views matched to the carousel it came from and scored against its account, which post format actually adds downloads (a regression with intervals), and a daily markdown dashboard with optional Notion sync. Use when someone asks "what moved this month", "why do trials cancel", "how are my TikTok posts doing", "which format brings installs", "build my morning dashboard", "App Store pulse", "trial funnel", or "sync my posts to Notion".
---

# App Growth Analyzer

Shows which TikTok posts brought App Store downloads, where trials cancel, and what changed
in the last 28 days. Writes a daily dashboard, optionally to Notion.

## Quick start

```bash
cd skills/app-growth-analyzer
pip install -r requirements.txt
cp .env.example .env              # fill in the ASC key; Notion only if you want it
printf 'yourhandle phone-1\nsecondhandle phone-1\n' > accounts.txt

python3 scripts/pulse.py          # 28-day window, saved, diffed vs the last snapshot
python3 scripts/trials.py         # trial funnel: paid / cancelled (which day) / card failed
python3 scripts/posts.py pull --captions 'tips=renders/tips/*/caption.txt'
python3 scripts/posts.py report   # accounts, best / worst, by format
python3 scripts/formats.py        # downloads per format, regression + intervals
python3 scripts/dashboard.py      # growth-data/dashboard.md
python3 scripts/dashboard.py --notion --dry   # what Notion would get; drop --dry to write
```

Every script answers `--help`. All data goes to `./growth-data/` (or `--data`, `GROWTH_DATA`).

## How it works

1. **Key.** App Store Connect → Users and Access → Integrations → Team Keys. Sales reports need
   the Sales/Finance or Admin role; analytics (impressions) needs Admin. Put key id, issuer id,
   the `.p8` path and the vendor number in `.env`. Never print them; the scripts never do.
2. **Accounts file.** One handle per line: `handle [label words] [id:<tiktok user id>]`. The
   label (e.g. which phone) shows on the dashboard. Add `id:` only when TikTok refuses to read
   a profile by its name.
3. **`pulse.py`** — pulls SALES + SUBSCRIPTION_EVENT for the window (cached per day), writes
   `snapshots/<date>.json`, prints each metric against the newest earlier snapshot with a %
   delta; `+`/`-` mark a 5%+ move in the good / bad direction. `--tiktok` adds follower / like /
   post totals per account from the public profile page.
4. **`trials.py`** — for every product with free trials: starts → paid, cancelled before paying
   (with a days-before-cancel histogram), billing retry (recovered / lost), refunds, cancelled
   after paying; plus subscribe / renew / cancel and median paid periods for every plan.
5. **`posts.py pull`** — yt-dlp reads every public profile (`--flat-playlist`, no login, no key),
   writes `posts.csv` (one row per post, whole history) and `history.csv` (views of young posts
   per pull, so you can read views at a fixed age later). **`report`** scores each post.
6. **`formats.py`** — regresses daily downloads on views per format; `--window` compares
   date ranges where you switched a format on or off.
7. **`dashboard.py`** — pulls posts + recent App Store days, writes one markdown page:
   App Store line from the last pulse, per-day funnel, accounts with quiet / low-views flags,
   formats scored. `--notion` also syncs posts into an "All posts" database and replaces the
   page. Run it every morning (cron / a scheduled agent).

## What to know

**App Store numbers**
- **Apple closes a day late.** Default window ends yesterday. A recent day is cached only once
  Apple has data for it; an empty recent day would otherwise freeze as empty forever. Days 3+
  old are cached even when empty (Apple answers 404 = no data).
- **The baseline is never today's own snapshot** — a second run the same day still diffs
  against the last real one. `--end` backfills an older window and stamps it with that date,
  so history sorts right. Only `[0-9]*.json` counts as a snapshot; keep other JSON out of the
  folder.
- **Step 14 days on a 28-day window.** A 7-day step shares most of its days with the last run,
  so every delta reads near zero and a real move drowns.
- **`Developer Proceeds` is per unit and in the local currency.** Multiply by `Units`, then
  convert. FX rates are hand-set and stamped with a date; an unknown currency counts 1:1 and
  prints a WARNING — fix it with `--fx rates.json`. An unconverted weak currency can inflate a
  total by orders of magnitude.
- **Proceeds per install = all proceeds / all first-time downloads** in the window. That is the
  number to hold against any cost of getting an install. Revenue per *payer* is a different
  number; do not mix them (see `references/method.md`).
- **The window's trial events are not one cohort.** A cancel in the window can belong to a trial
  that started before it. Read shares as a trend; for a clean cohort end the window one trial
  length ago.

**Trials** — read `trials.py` like this:
- A "your trial ends tomorrow" reminder cannot reach a day-0 canceller. If most cancels land on
  day 0, do not expect it to move cancels; the decision happens in the first minutes.
- Hold trial → paid against a public benchmark for your trial length before calling the
  conversion broken. A failed-card share is often structural (a young audience), not a paywall bug.
- If cancels after paying are ~0, churn lives in the trial, not after it.

**Posts**
- **Match = exact hashtag set.** Every caption you post must carry a set no other caption has.
  The match is stored on the post the first time a pull sees it, so it survives deleting old
  render folders — **pull daily**.
- **Score = views / the same account's median** in the window, posts 3+ days old only. That
  cancels account luck. The dashboard wants ~48 posts per value (default) before it reads a 2x
  gap; smaller gaps need far more. It prints how many are missing.
- **A wrong handle scrapes fine and silently poisons the totals.** Check each handle once.
- **Views per like differ a lot across accounts.** Likes are a trend line, never a payout basis. `pulse.py --tiktok` only estimates views when `posts.csv` gives your own ratio.
- yt-dlp's `description` holds the hashtags only, not the caption's first line.

**Formats → downloads**
- TikTok passes no install source on. The only per-format number is a regression of daily
  downloads on views by format, downloads lagged 1 day. The constant is the **base**: downloads
  you get with zero posts — often people searching the app's name: built-up exposure, not
  today's posts.
- So a dashboard "downloads per 1k views" is base ÷ views, **not** what one post brings.
- Trust a format only when its 90% interval stays on one side of 0 across most of the 8
  variants.
- **Switching a format on or off is the only clean experiment.** Use `--window`.
- Two formats that always ran together cannot be separated.

## Cost

Free. App Store Connect, yt-dlp and Notion calls cost nothing. A first 28-day pulse makes ~56
report calls (~1 min); a repeat reads the cache (seconds). yt-dlp: ~5–20 s per account. Notion:
~3 requests/second, so a first fill of a long post history takes minutes.

## Limits

- Only GET calls to Apple. Notion writes only with `--notion` (and never with `--dry`); it adds
  and updates rows, never deletes, and never passes `allow_deleting_content`.
- Share the Notion page with the integration (page ••• → Connections) or every call is a 404.
- Impressions need an ONGOING analytics report request id (`ASC_ANALYTICS_REQUEST_ID`); Apple
  builds instances 24–48 h after the request exists. None yet is not an error.
- Apple's docs list the Subscription Event report as version `1_3`; live accounts answer `1_4`.
  If you get HTTP 400, set `ASC_EVENT_VERSION=1_3`.
- Private, banned or renamed TikTok accounts show as FAILED / "can't read"; posts of an account
  removed from the accounts file stay in `posts.csv` (it is the whole history).
- yt-dlp follows TikTok's site; if it breaks, `pip install -U yt-dlp`.

More on method, LTV definitions and test sizes: `references/method.md`.
