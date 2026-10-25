# Lessons and output fields

## Finding viral carousels

- Top search: only part of its results are photo carousels, and they lean old. A 180-day window still reads as
  "now" for a format.
- Expect only a small share of raw results to survive the 3x-median filter.
- A phrase's yield varies a lot. Keep the phrases that produce hits, and take new ones from the winners' captions.
- Apify's clockworks scraper returns 0 items for the `/video` search section combined with a date filter. Use Top.

## Scoring

- Score = views ÷ the same account's median in the same calendar month. Fall back to the account median when that
  month has fewer than 5 posts.
- Why: account hot spells swamp format. On a plain account index, a format detail can look several times better
  than it is with month control.
- Only compare posts that have finished being shown: leave out posts younger than about 3 days.

## How networks are built

- **Hidden partners**: accounts that look personal but name the app on almost every carousel, from their first
  post to their last. Check several posts spread across an account's life.
- **Finding them**: a creator tag (`#<app>partner`), a handle pattern, a shared bio line, and the same cover
  template. A hashtag alone misleads: random users use it too, and a top partner may barely use it.
- **Mention position**: usually a middle slide, the 2nd-last slide, or the last slide or last list item. Almost
  never the cover. Rarely a last-slide CTA only.
- **Mention look**: most often text only, in the same style as the body slides; sometimes app screenshots or
  App Store cards.
- **Mention wording**: most mentions name one concrete feature. Many give a reaction as proof, open as a skeptic
  ("tried it as a joke"), or compare the app to a known thing. Lines are often reused word for word across many
  posts.
- **Mention slide length**: it carries more words than the other slides.
- **Cover reuse**: some networks repost the same cover text and cover photos many times; others never do.
- **Comment seeding**: "I use it too!!" comments can come from the network's own other accounts, found by
  matching handles. Real viewers ask about price, "is it free", and Android.
- **Boost sign**: a creator's app posts getting far more views than the same creator's other posts points to paid
  promotion.
- **Same template across apps**: different apps copy one cover template.

## Views versus installs

- Public conversion numbers are rare and biased to winners. Use your own views-per-install figures; high reach
  does not prove installs.
- Ratings gained per day (`teardown.py appstore`, re-checked a few days later) is the cleanest public growth
  signal.

## Creator pricing

- The CPM is your input (`--cpm`); the tool has no default rate. `--cap-usd` caps the pay per video (no cap by
  default).
- The tier-1 share comes from the creator's own analytics (audience by country).
- Public views are all-time. A video over the cap is an upper bound on what the deal window held.

## Tools

- yt-dlp `--flat-playlist -J` returns, per post: `view_count`, `like_count`, `comment_count`, `repost_count`,
  `save_count`, `track`, `timestamp`, `description` (hashtags only), and `thumbnails`. It is fast: a whole
  profile in seconds.
- Profile HTML (`__UNIVERSAL_DATA_FOR_REHYDRATION__` → `webapp.user-detail.userInfo.statsV2`) gives exact
  follower, like and video counts. Its `itemList` is always empty, and post pages carry no stats.
- tikwm endpoints in use: `api/?url=` (slide links, caption), `comment/list`, `challenge/info`, and
  `challenge/posts`. `user/info` returns 403.

## Output fields

- `find.py pick` → `hits.json`: id, url, account, followers, bio, bio_link, caption, posted, views, likes, saves,
  comments, shares, like_pct, save_pct, score, slides, sells, query, sheet.
- `account.py` → `posts.csv`: id, account, posted, kind (carousel/video), views, likes, comments, shares, saves,
  score, sound, hashtags, url. `accounts.json` holds the per-account summary.
- `teardown.py` → `ocr.json` `{post_id: [{w, h, lines: [[text, x, y, w, h]]}]}`. Boxes are normalised to 0–1,
  with the origin at the top-left. `carousels.json` has one row per carousel: idx, n, landscape, cover, slides,
  words, app_slides, app_caption, cta_last.
- `creator_price.py` → `creator_price.json` / `.md`.
