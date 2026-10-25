---
name: tiktok-auto-poster
description: Deal folders of finished TikTok photo carousels fairly across phones and accounts, send them to Telegram for an OK, then post them as TikTok inbox drafts through Postiz Cloud, as Telegram albums for hand posting, or as one-shot tasks on an iPhone farm, on a daily schedule. Idempotent via marker files, dry run by default. Use when someone says "post my carousels", "schedule carousels to TikTok", "deal posts across accounts", "send carousels to Telegram for approval", "TikTok drafts via Postiz", "push carousels to the phone farm", or "daily TikTok posting routine".
---

# TikTok Auto Poster

Splits finished carousels across your accounts, asks your OK in Telegram, then posts them as
TikTok drafts (Postiz), hand-post albums, or through an iPhone farm.

It doesn't make carousels. A carousel here is any folder holding images (slide order = file name
order) and an optional `caption.txt`: first paragraph = TikTok title, the rest = description
and hashtags.

## Quick start

```bash
pip install -r requirements.txt
cp .env.example .env                 # fill in only the routes you use
cp accounts.example.txt accounts.txt # your handles, devices, routes
S=scripts/poster.py

python3 $S hold    --dir renders/a --dir renders/b --new           # every new carousel waits for an OK
python3 $S plan    --dir renders/a --dir renders/b                 # print the deal, change nothing
python3 $S plan    --dir renders/a --dir renders/b --save          # write it (.plan in each folder)
python3 $S review  --dir renders/a --dir renders/b --live          # held ones -> Telegram, labelled device + @handle
#   ...you reply OK (or name the ones to fix)...
python3 $S approve --dir renders/a --dir renders/b --all           # lift holds that were reviewed
python3 $S postiz  --dir renders/a --dir renders/b --live          # postiz accounts: inbox drafts at 12:30 their time
python3 $S telegram --dir renders/a --dir renders/b --live         # telegram accounts: albums to post by hand
python3 $S farm    --dir renders/a --dir renders/b --live --limit 1   # farm accounts: one-shot tasks (start with 1)
python3 $S check                      # Postiz posts of the last 2 days, failures first
```

Every command answers `--help`. Everything that sends, books or pushes is a **dry run unless
`--live`** is given — the dry run prints exactly what the live run will do.

## The accounts file

One account per line, in the order you want them dealt (see `accounts.example.txt`):

```
handle            device name     route
demo.account.one  phone 1                     # route left out = telegram
demo.account.two  phone 1         postiz
demo.account.five phone 3         farm
```

- **device** groups accounts that live on one phone. The deal makes each device post a mix of
  formats, so it matters even if you never touch the phones yourself.
- **route** decides who posts it: `telegram` (album to your chat, a person posts it),
  `postiz` (TikTok inbox draft via Postiz Cloud), `farm` (task on an iPhone farm).
- handles must match the Postiz channel's profile name / the farm's account list exactly.

## How it works

1. **Hold.** `hold --new` marks every folder that was never held, approved or posted. Better:
   have your renderer write `.hold` itself the moment it creates the folder (rule 1).
2. **Plan.** `plan` deals today's slots: `--per-account N` posts per account (default 1).
   Devices in file order, accounts in file order inside a device, and the source folder
   rotates by device, account and round. Two formats on two accounts per phone come out
   `a b` on phone 1, `b a` on phone 2. A folder that runs dry is topped up from the others.
   `--save` writes the assignment into each folder; re-running only fills empty slots.
   Extra carousels wait in the pool. A carousel planned but not posted on its day goes back
   to the pool and may land on another account next time.
3. **Review.** `review --live` sends each held carousel of today's plan to Telegram
   (`TELEGRAM_REVIEW_CHAT_ID`, else `TELEGRAM_CHAT_ID`) as `REVIEW 3/8 · phone 2 · @handle ·
   id`, then the album, then one line asking for OK. Writes `.reviewed`.
4. **Approve.** Wait for the human. `approve --all` lifts only holds that were reviewed
   (`--unseen` overrides), `approve --ids a b` lifts named ones. To drop a carousel for good,
   move or delete its folder; a held one keeps its slot only for that day.
5. **Post.** `postiz`, `telegram`, `farm` each act on today's plan rows for their route that
   are approved and not already done, and write `.postiz`, `.sent`, `.farm`. Run them in any
   order, as often as you like.
6. **Check.** `check` lists Postiz posts, failures (`ERROR`) first. Report every
   `POSTIZ FAILED` line from step 5 word for word; the carousel stays unbooked for the next run.

Markers live inside each carousel folder:

| marker | written by | means |
|---|---|---|
| `.hold` | `hold`, or your renderer | not approved — nothing posts it |
| `.plan` | `plan --save` | day, device, handle, route (json) |
| `.reviewed` | `review --live` | review album went out |
| `.approved` | `approve` | hold lifted |
| `.sent` / `.postiz` / `.farm` | the route commands | done; never sent again |

## What to know

1. **The `.hold` check is the whole safety of the approval gate.** Hold a folder the moment it
   exists, not after the batch finishes: a batch render can straddle the send time, and every
   folder without a hold in that window can post itself unreviewed.
2. **A re-render into an existing folder must delete the old markers.** Folder names are often
   the headline slug, so a topic that comes back lands on the same path and inherits the old
   `.sent` — it is then silently skipped.
3. **One chat per send, never a list.** Mirroring one send to two chats lands every carousel
   twice.
4. **A retry must rewind the files.** Albums are big and the link to Telegram drops, so uploads
   retry — and a retry that re-posts already-read file handles uploads empty files that
   Telegram accepts without complaint.
5. **Mark done only after everything went through** (label and album both), so a crash halfway
   never makes a carousel look sent.
6. **Rotate, don't randomise.** A coin flip makes the dry run print a plan the live run then
   contradicts. Interleave (`a b a b`) instead of blocks (`a a b b`): blocks give one account
   near-identical posts back to back.
7. **Drafts, not direct posts, on Postiz.** No public API attaches a sound you choose to a
   photo post. Direct posts with `autoAddMusic=yes` get random tracks from TikTok's
   Commercial Music Library. A draft keeps only title and
   description; a person adds the sound in the app and taps Post. Details:
   `references/postiz.md`.
8. **The Postiz create call is never retried.** A 5xx can still have created the post; a retry
   books it twice. Uploads and reads do retry.
9. **Farm: a state check before every tap, or don't run it.** Blind coordinate taps can
   publish a Story while the task logs "draft saved — succeeded". Read
   `references/farm.md` before the first `farm --live`.
10. **One-shot tasks only on the farm.** A recurring task re-posts the same media every time.
11. **Keys in a `.env` file, not your shell.** Scheduled runs do not source your profile. A
    real env var wins over the file; an empty value in the file never wins.

## Images

- Postiz route: each image is re-encoded to JPEG (TikTok photo posts take JPEG/WebP only),
  shorter side scaled to 1080 px max, metadata dropped. 1–35 images per post.
- Telegram: images go as documents (no recompression), ≤10 per album, so 12 slides become an
  album of 10 then 2; the caption hangs under the last file as two tap-to-copy code spans.

## Scheduling

Run it from cron or a Claude Code scheduled task. See `references/scheduling.md` for a crontab
and a scheduled-task prompt. The shape: render at night → `hold` + `plan --save` + `review
--live` in the morning → a person replies OK → `approve` → the route commands.

## Cost

- Telegram: free.
- Postiz Cloud: paid per channel bucket (list prices 2026-10: $29 / 5 channels, $39 / 10,
  $49 / 30, $99 / 100). Each carousel = one upload call per image + one create call.
- Farm: your phones and a Mac; the farm software is open source.
- This skill spends nothing without `--live`.

## Limits

- Postiz public API documents **30 requests per hour**. A 6-image carousel is 7 calls. The
  script backs off on 429 and the failed booking is retried on the next run; whether uploads
  count against the limit is untested.
- TikTok inbox drafts: at most 5 pending per account per 24 h (TikTok Content Posting API).
- Self-hosted Postiz cannot publish public TikToks (`references/postiz.md`).
- No per-format balancing beyond folder rotation; put formats in separate `--dir` folders.
- The farm client expects the HTTP API shape in `references/farm.md`; check yours.
