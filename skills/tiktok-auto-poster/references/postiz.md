# Postiz route — API calls and settings

API reference: https://docs.postiz.com/public-api — base `https://api.postiz.com/public/v1`,
header `Authorization: <raw key>`.

| call | used for |
|---|---|
| `GET /integrations` | connected channels; matched to your handle by `profile`, `disabled` ones skipped |
| `POST /upload` (multipart `file`) | one call per image → `{id, path}` |
| `POST /posts` | `type: schedule`, UTC `date`, one post per channel, `settings.__type` = the channel's `identifier` |
| `GET /posts?startDate&endDate` | `check`: state `QUEUE` / `PUBLISHED` / `ERROR` / `DRAFT` |

## Why Postiz Cloud and not self-hosted

TikTok's content-sharing guidelines: an **unaudited** API client posts private-only
(`SELF_ONLY`), max 5 users per 24 h. The audit lists "a utility tool to help upload contents
to the account(s) you or your team manages" as unacceptable. So your own TikTok developer app
behind a self-hosted Postiz cannot publish publicly. Postiz Cloud posts through its own audited
app. On Cloud the only TikTok tile is the `tiktok-business` provider; the script passes
whatever `identifier` the channel reports.

## Why drafts (`content_posting_method: UPLOAD`)

- No API field takes a sound you pick for a photo post. The Business provider can attach a
  Commercial Music Library track, chosen by genre only; the trending lists came back as
  library/stock music, and some genres were empty.
- Direct posts with `autoAddMusic: yes` get random library tracks.
- In a draft TikTok keeps only title and description. The other settings are still required
  by the Postiz schema and are ignored. The draft lands in the account's TikTok inbox; a
  person opens it, adds the sound and taps Post.

## The slot

Default `--at 12:30 --tz America/New_York`. Set your audience's time zone. No jitter: a draft goes live when a
person taps Post, not when it lands, so all drafts arrive together. A slot closer than
10 minutes is pushed to now + 10 min (Postiz needs time to pull the photos; a past date
publishes at once).

## Failures

- `Failed to pull photo from URL` can happen on a first booking — retry on the next run.
- A render with a 1440 px short side is rejected by TikTok's photo rules — the script scales
  to 1080.
- A failed booking prints `POSTIZ FAILED @handle folder: reason`, writes no marker, and the
  command exits 1. Nothing is sent to Telegram for it.

## Not tested

- Whether `POST /upload` counts against the documented 30 requests/hour.
- Self-hosted Postiz (`POSTIZ_API_URL`) beyond the base URL swap.
