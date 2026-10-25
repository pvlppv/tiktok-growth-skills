---
name: tiktok-viral-researcher
description: Research TikTok photo carousels. Give it a niche and it finds the carousels that went viral there, each scored against its own account's median. Give it a rival's accounts and it pulls stats for every post for free, reads every slide with OCR, and maps how the posts are built and how they mention the app (which slide, what wording, reused covers, comment seeding). It also prices a creator's views and checks them against public numbers. Use for "find viral carousels in <niche>", "what formats work for <topic>", "tear down this account / app network", "how does <rival> mention their app", "stats for @handle", "is this creator worth $X", "price these creator views".
---

# TikTok Viral Researcher

Finds viral carousels in your niche, pulls stats for any account, reads competitors' slides,
and checks what a creator's views are worth.

Four commands, all in `scripts/`, all answer `--help`:

| command | what | cost |
|---|---|---|
| `find.py search` then `find.py pick` | viral carousels for a niche | search is **paid** (Apify), pick is free |
| `account.py` | every post's stats for any public account | free (yt-dlp) |
| `teardown.py` | slide-by-slide OCR read of an account or a network | free (yt-dlp + tikwm + OCR) |
| `creator_price.py` | price a creator's views, check them against public data | free (yt-dlp) |

Output: markdown reports, slide contact sheets (JPG), and JSON/CSV next to them.

## Quick start

```bash
pip install -r requirements.txt            # yt-dlp, Pillow; Vision OCR on macOS
cp .env.example .env                       # only find.py search needs APIFY_TOKEN

# 1. viral carousels for a niche (paid search, then free filter)
python3 scripts/find.py search --query "study tips" --query "glow up routine" --per 25 --max-usd 1
python3 scripts/find.py pick --top 20      # -> tiktok-research/picks/<stamp>/hits.md + sheets

# 2. stats for any public account
python3 scripts/account.py @handle1 @handle2 --profile   # -> tiktok-research/accounts.md, posts.csv

# 3. tear down a rival account or network
python3 scripts/teardown.py --dir rival tag someapppartner         # who posts under the creator tag
python3 scripts/teardown.py --dir rival pull @acc1 @acc2 --days 200
python3 scripts/teardown.py --dir rival ocr
python3 scripts/teardown.py --dir rival apps                       # which app names the text carries
python3 scripts/teardown.py --dir rival report --app "some ?app"   # -> rival/report.md + sheets/
python3 scripts/teardown.py --dir rival comments --top 40 --app "some ?app"
python3 scripts/teardown.py --dir rival appstore "some app"

# 4. price a creator
python3 scripts/creator_price.py @creator --tier1 60 --claimed 100000 --since YYYY-MM-DD --cpm CPM --asked 100
```

## How it works

**Niche → viral carousels**
1. Write 5–15 search phrases the way a viewer types them (the topic, not the app). Start small: `--per 25`
   costs about $0.09 per phrase.
2. `find.py search ... --max-usd N`. Apify stops at N on its side. Raw results are saved, so the next step can be re-run for free.
3. `find.py pick`. It keeps photo carousels with 2+ slides that are not ads, are from the last 180 days, and have 20k+ views.
   Each one must also beat its own account's median views 3x, or have 500k+ views. Medians are read free with yt-dlp
   and cached. Posts shown in an earlier pick are skipped (`seen.txt`).
4. Read `hits.md` and look at the sheets. `sells = yes` means the bio links somewhere or the caption talks about an app,
   which is a hint the account is a paid or hidden partner. Run step 3 on those accounts.

**Rival → teardown**
1. Find the accounts. Look for a creator-program tag (`teardown.py tag <name>partner`), a handle pattern
   (`<name>.<app>`, `<name>_<niche>_growth`), a shared bio line, and the bio link. A tag is not the network: check
   every account it gives you.
2. `pull` reads each account's history (yt-dlp), then fetches slide links for each carousel (tikwm).
3. `ocr` downloads the slides and reads every text line with its box. It uses Apple Vision on macOS and tesseract elsewhere.
4. `apps` lists the app names the captions and slides carry. Pick the real one, then write an app-specific regex.
5. `report --app REGEX` shows, per account, how many carousels name the app (a partner names it on most of them).
   It also covers slide count, frame, number on the cover, last-slide CTA (call to action), reused cover texts,
   where the mention sits, the words on the mention slide, and the mention openings and top mention sentences.
   Sheets of the top posts come with it.
6. `comments` shows who praises the app in the comments. If those commenters are the network's own accounts,
   the comments are seeded. It also counts how many real viewers ask "what app / is it free".
7. `appstore` gives the exact ratings count for each country. Re-check it a few days later: ratings gained
   per day is the cleanest public growth signal.

**Account stats**: `account.py` reports per-account posts, carousels vs videos, posts per day, and median views
(all / last 30 days / first 30 days). It marks accounts active (posted in the last 14 days) or stopped (nothing
for 21+ days), shows a months table, and lists the best posts by score. With `--profile` it adds followers and
the bio link.

**Creator price**: price = views × tier-1 share ÷ 1000 × CPM (cost per 1,000 views). `--cpm` is your rate and
is required; `--cap-usd` caps the pay per video (no cap by default); `--views-per-install LO-HI` adds a cost per
install range from your own figures (off by default). The public pull is the check: public views should be ≥ the claimed views, the like rate should look human, and a big
gap between profile likes and visible-post likes means deleted posts.

## What to know

More detail is in `references/lessons.md`.

- **Score against the account itself, by month.** Account hot spells swamp format: on a plain account median a
  small detail can look far better than it is. `teardown.py report` and `account.py` both divide by the same
  account's median for the same month.
- **20k views and 3x the account median.** A 3x hit on a 300-view account is noise. Big hits (500k+) are kept
  whatever the ratio.
- **Top search only, 180-day window.** Top is the only search section that returns photo carousels, and only
  part of its results are carousels. It also leans old.
- **yt-dlp gives stats, not slides.** For photo posts it returns audio only. A carousel is a post whose
  thumbnail URL contains `photomode`. Slides come from tikwm.
- **Read the creator tags.** App-name detectors miss `#<app>partner` tags. A topic regex ("glow up") matches the
  topic, not the plug. Use words only the app has.
- **Hidden partners name the app on almost every carousel, for the account's whole life.** Usually as text only,
  on a middle or 2nd-last slide, or as the last item of a list. The mention slide carries more words than the others.
- **Covers get reused.** Winning templates get reposted; `report` counts cover texts used 3+ times.
- **Check who comments.** Seeded "omg I use it too" comments can come from the network's own other accounts.
  Real viewers ask about price, "is it free", and Android.
- **Views are not installs.** Every index here counts views. Public conversion numbers are rare.

## Cost and limits

- **Apify** (`find.py search` only): about **$0.0037 per result** on the free tier (check your plan). The
  smallest cap is $0.50 per run, and `--max-usd` is required. Only a small share of raw results survives `pick`.
  Slide links in raw results expire within days, so build sheets soon after the search.
- **tikwm** (free, no key): 1 request a second, enforced in `tt.py`. There is a **~10,000 calls/day cap shared by
  your IP**, reset around 00:00 UTC. Parallel jobs slow it to about 0.4 posts/s.
  Everything is resumable. `user/info` returns 403; bios come from the profile page instead.
- **yt-dlp** (free, no login): 5–20 s per profile. Read 4–6 profiles at a time; if you hit a bot wall, run fewer
  at a time. Some profiles refuse by name: pass
  `tiktokuser:<numeric id>`. Big accounts: `--cap 600`. The listing is sometimes partial, so re-run if
  posts look missing. `description` holds hashtags, not the full caption.
- **Profile page** (followers, total likes, bio link): exact integers from the public HTML. It has no per-post views.
- **OCR**: Apple Vision (`pyobjc-framework-Vision`) is **tested**. The tesseract fallback is written but **not
  tested**: it needs `brew install tesseract` / `apt install tesseract-ocr`. Slides are about 0.25 MB each;
  `--delete-slides` saves the disk space but then there are no sheets.
- **Speech**: no speech-to-text, so a video that only says the app out loud counts as "not naming it".

## Files

- `scripts/tt.py`: shared helpers for yt-dlp, tikwm pacing, Apify runs, and contact sheets.
- `scripts/find.py`, `account.py`, `teardown.py`, `creator_price.py`: the four commands.
- `references/lessons.md`: the lessons in full, plus the output fields.
