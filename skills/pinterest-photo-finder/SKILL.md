---
name: pinterest-photo-finder
description: "Search Pinterest for free (no login, no API key), download every hit at its original full size, drop the blurry, too-small, off-brightness and duplicate photos, and hand back numbered contact sheets so a human can pick the keepers. Trigger when the user wants background or cover photos for carousels, slideshows or posts, asks to 'find aesthetic photos of X', 'restock / refill my photo pool', 'get HD Pinterest images', 'build a dark photo pool', or wants a photo set de-duplicated against what they already have."
---

# Pinterest Photo Finder

Finds full-size Pinterest photos, drops blurry, duplicate and off-brightness ones, and makes
numbered sheets to pick from.

```bash
pip install -r requirements.txt
# 1. test a query cheaply (1 page = ~50 pins)
python3 scripts/pinterest_photos.py pool/ --q "candle lit room aesthetic" --pages 1 --sheet
# 2. real hunt: several queries, a dark brightness band, against what you already have
python3 scripts/pinterest_photos.py pool/ --pages 3 --lum 0 110 \
    --q "moody bedroom night lamp" --q "books and incense aesthetic" \
    --against old_pool/ --against cut/ --sheet
# 3. sheets only (after you deleted some files)
python3 scripts/pinterest_photos.py pool/ --sheet
```

Output: `pool/<md5>.jpg` (upright RGB, no EXIF rotation tag), `pool/_sources.jsonl`
(file → query → URL), `pool/_sheets/1.jpg, 2.jpg…` (24 numbered tiles each) and
`pool/_sheets/index.txt` (number → file). The run ends with rejects counted by reason:

```
  N kept  <- candle lit room aesthetic
{'KEPT': N, 'size/ratio (listed)': N, 'blurry': N, 'repeat': N, 'download': N}
```

## Flags

| Flag | Default | What it does |
|---|---|---|
| `out_dir` | — | where kept photos go; its photos also count for duplicates |
| `--q` | — | search words; repeat for more queries |
| `--pages` | 3 | result pages per query, ~50 pins each |
| `--lum MIN MAX` | 0 255 | mean brightness band (0–255). `0 110` = dark pool |
| `--min-side` | 900 | shortest side in px |
| `--min-ratio` | off | min width/height. `0.74` drops tall pins, `1.3` = landscape only |
| `--sharp` | 45 | min sharpness (p99 \|Laplacian\| at 720px wide) |
| `--against DIR` | — | folders whose photos count as already had (your pool, your cut list) |
| `--dedup` | auto | `vision` (macOS) or `phash` (pure Python). auto picks Vision when present |
| `--same` | 0.37 / 10 | duplicate cut: Vision L2 distance / phash Hamming bits |
| `--sheet` | off | write numbered contact sheets |
| `--tile` | 480 | sheet tile width in px |

## How it works

1. **Ask for the look, not the words.** What the photos are for (cover, background behind
   text, hook), the mood, and 3–5 reference photos the human already likes. Turn that into
   5–10 concrete queries — objects and places ("wired earphones aesthetic",
   "girl in cafe aesthetic"), not adjectives.
2. **Probe every new query at `--pages 1`.** Some return nothing or the wrong thing. Keep the
   queries that keep 10+ of 50; drop the rest.
3. **Hunt** with the survivors at `--pages 3–4`, with `--against` on the existing pool and
   on your cut folder.
4. **Show the sheets.** The agent opens `_sheets/*.jpg`, flags the obvious misses by number
   (watermarks, text, wrong subject), and the human picks. Never move photos into a live pool
   without that pass.
5. **Move the cuts to a cut folder** (don't delete them) and pass it as `--against` next time,
   so they can never be downloaded again.

## What to know

- **The search API needs the search page first.** `BaseSearchResource/get` answers 403
  "Invalid Resource Request" without the cookies the `/search/pins/` page sets, without the
  `X-Pinterest-PWS-Handler` header, or with `source_url` not fully percent-encoded. The
  script does all three. If the page fetch fails the API often still works.
- **Originals = HD.** `images.orig.url` (`i.pinimg.com/originals/…`) is the biggest file
  Pinterest stores; there is no bigger version to chase.
- **Big is not sharp.** Many originals are upscales. Sharpness = 99th percentile of
  |Laplacian| after resizing to 720px wide; the default cut is 45 (below it a photo looks
  blurry at zoom). Resizing first makes the score size-independent.
- **"jpg" files can be HEIC**, and some come out sideways. The script loads HEIC
  (pillow-heif, else macOS `sips`), bakes EXIF rotation into the pixels and saves without the
  tag, so every viewer shows it the same way up.
- **No pin IDs.** Search returns `e1_…` tokens, not real pin ids, so files are named by the
  hash of their bytes and the source URL is logged in `_sources.jsonl`.
- **Duplicates need a real image print.** Apple Vision feature print, L2 distance; default
  cut 0.37 (at or below = same photo). Vision catches crops; phash only catches re-uploads
  and resized copies. A small average hash misses crops too. So on macOS keep Vision
  (`pip install pyobjc-framework-Vision`); elsewhere expect some repeats on the sheet.
- **Pinterest is portrait-heavy.** `--min-ratio` drops many pins; landscape-only needs
  many more pages. Listed-size rejects happen before download, so they cost nothing.
- **Yield is low and query-driven.** Expect few keepers per page for a narrow subject.
  Broad queries drift off the subject; specific ones work. Some words return zero results —
  that is what the `--pages 1` probe is for.
- **A brightness band is a pre-filter, never the judge.** For a dark pool, `--lum 0 110`.
  A script can drop the extremes, but kept and cut photos overlap in brightness and
  saturation; taste is the human pass.
- **Check at ~500px, not thumbnail size.** Misses that hid in small tiles: the wrong number
  of people (aerial shots hide it best), paintings and toy figures passing as photos.
  Also cut by eye: watermarks, emoji or date stamps, printed text overlays, grey border
  frames, AI renders when the look must be "shot on a phone".
- **The eye decides; the agent ranks.** Do not guess someone's taste. Show a sheet, take the
  picks, and write the taste down as concrete keep/cut examples before the next hunt.
- **If text goes on top, test placement before the pool.** Many eye-approved photos have no
  calm area for a text block. Run your own layout check on the kept set before it goes live.

## Cost

Free: no key, no login, no paid service. Speed is the limit: downloads run one at a time,
a few minutes per 50-pin page on a home connection.

## Limits

- An unofficial endpoint. It can change or start answering 403 without notice; the request
  shape above was last confirmed working on the day this skill was written.
- No pin links, no board or author info — only the image URL.
- Vision dedup is macOS only; phash is the weaker fallback (see above).
- Gates judge pixels, not content: subject, people count, text and taste are the human pass.
