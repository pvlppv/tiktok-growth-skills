---
name: tiktok-slideshow-generator
description: "Render finished TikTok / Instagram photo-carousel slides from slide text and a folder of photos. Places the text by Apple Vision saliency with hard gates so it never lands on a face, skin, printed text, an object or a busy surface; picks the crop, ink colour, dim and size per slide; uses every cover photo once; makes every file byte-unique and metadata-free; writes a preview sheet and a per-slide check report. Trigger when the user wants to 'make a TikTok slideshow', 'render carousel slides', 'put text on these photos', 'turn this script into slides', 'batch carousels from a photo pool', 'check my slides are unique before posting', or asks why text keeps landing on faces."
---

# TikTok Slideshow Generator

Turns your text and photos into finished slides. Text never lands on faces or objects, each
cover is used once, every file is unique.

You write the words. The engine picks the photo, crop, ink, dim, size, column and every
coordinate. Nothing is posted and nothing costs money: it runs fully on your machine.

## Quick start

```bash
pip install -r requirements.txt
# a free bold font (OFL): Montserrat or Inter from fonts.google.com — none is shipped
FONT=~/fonts/Montserrat-Bold.ttf

# 1. validate script + pools (fast, nothing touched)
python3 scripts/slideshow.py check  references/example-script.json \
    --covers covers/ --backgrounds backgrounds/ --out out/
# 2. odds per slide on your real pool, no render, nothing consumed
python3 scripts/slideshow.py probe  references/example-script.json \
    --backgrounds backgrounds/ --font $FONT
# 3. render (consumes one cover photo)
python3 scripts/slideshow.py render references/example-script.json \
    --covers covers/ --backgrounds backgrounds/ --font $FONT --out out/
# 4. prove every file is unique + metadata-free before anything is posted
python3 scripts/verify.py out/
```

Landscape (1920x1440, the "photo mode" look) instead of portrait 1080x1920: `--frame landscape`.
Colour emoji: `--emoji-font "/System/Library/Fonts/Apple Color Emoji.ttc"` (or NotoColorEmoji).

## Inputs

**Script** — one JSON per carousel:

```json
{
  "id": "desk-reset-five-steps",
  "cover": ["the 10 minute desk reset\nI do every friday", "(it fixes monday)"],
  "slides": [
    {"head": "1. clear everything off", "body": "all of it, onto the floor"},
    {"head": "save this for friday", "photo": "photos/closer.jpg"}
  ],
  "caption": "what is on your desk right now?",
  "hashtags": ["deskreset", "workspace"],
  "inks": ["white", "yellow"]
}
```

| Field | Need | Meaning |
|---|---|---|
| `id` | no (file name) | output folder name; **never overwritten** — render refuses an existing id |
| `cover` | yes | `[hook]` or `[hook, subtitle]`; `\n` forces a line break |
| `slides[].head` / `.body` | one of them | head = bold line, body = smaller lines under or beside it |
| `slides[].photo` | no | pin a photo (path relative to the script); else picked from the pool |
| `cover_photo` | no | pin the cover; a pinned cover is not consumed |
| `inks` | no | pin head/body inks from `white`, `yellow`, `black`; default = audition |
| `caption`, `hashtags` | no | written to `caption.txt` |

**Pools** — two folders of jpg/png/webp/heic:

- `--covers` — **consumed**. The winning cover moves to `covers/used/` only after a successful
  render. Exact and near re-downloads (256-bit dhash, distance ≤ 8) of a used photo are skipped.
- `--backgrounds` — reused across posts, **never twice in one post**. Photos used often in
  earlier `out/*/meta.json` pay a freshness fee, so calm photos do not win every post.

## Output

`out/<id>/`: `01.png` (cover) … `NN.png`, `caption.txt`, `meta.json` (seed, inks, dim, photo +
crop rect + status per slide), `report.txt`, `_sheet.jpg` (all slides on a tenths grid, centre
line red). Work happens in `out/.<id>.partial/`; a crash leaves no half folder under the real id.

Report line per slide: `one` (one block) / `split` (head and body apart) / `weak` (ink under
floor) / `fallback` (no legal spot). Last line:
`placement: 6/7 clean (1 split)   1 need a human`. Only flagged slides need your eye.

## How it works

1. **See each photo once.** Apple Vision in one batched call: faces, person mask, attention +
   objectness saliency, printed text, labelled objects. Plus per-pixel edges and skin colour
   (skin = chroma near this person's own face).
2. **Plan the crop in the source.** Slide the output window over the photo; a window that
   slices a face is near-banned, keeping a whole face earns a bonus. Near-ties are broken by a
   seed hash, so two posts of one photo get different crops.
3. **Build the gate map.** Forbidden = skin, faces, printed text, labelled objects, object
   edges (gradient > 45; 95 on the cover), a seam between two surfaces, a surface the ink
   cannot be read on. Everything forbidden grows a 1.2% pad. A text block may touch **zero**
   forbidden cells. Gates refuse, they do not charge.
4. **Search.** Size ladder × width ladder × column. No legal spot: split head from body (under
   or beside). Weak ink: retry with that spot blocked (≤ 3). Still nothing: walk to the next
   ranked photo (14 auditioned per slide). Only the last photo may take an ungated spot, and
   that slide is flagged.
5. **Pick inks for the whole post.** Head/body pairs are tried in a seeded order; the first pair
   with zero flagged slides wins. Ink must clear its floor on mean ± 1.3σ of the pixels under
   it, not just the average.
6. **One dim per post.** Highlights-only curve `out = in·(1 − k·(in/255)²)`, k from the
   neediest slide, rounded up to 0.05, capped at 1/3, floor 0.18 with light ink. Same exposure
   on every slide reads as one set.
7. **Make every file unique.** Small regrade (temperature, hue, contrast, saturation) plus
   sub-visible jitter, then a fresh pixel-only image: no EXIF, no text chunks, no ICC.

## What to know

- **Body length is the lever.** A longer body finds a legal spot on far fewer photos. `check`
  warns above 80 chars. Cut words before you add photos.
- **Flagged odds = slides × (1 − p)^audition**, p = share of photos with a legal spot. `probe`
  prints p per slide on your pool and says `<- cut the body` when the odds pass 5%.
- **Split rescues most homeless slides.** When a full block fits nowhere, the head alone or the
  body alone often still fits. That is why split exists, not as a style.
- **A flag means re-render once, then edit the copy.** A new seed picks other photos and crops;
  most flags clear. Two flags on the same slide = the text is too long for the pool.
- **Count covers before planning a batch.** One cover per post. No fresh cover = hard stop.
- **Restock = move `covers/used/*` back.** There is no undo command; nothing else tracks covers.
- **Never run two renders on one cover pool at once.** Both can pick the same cover.
- **Pick ids that are new.** The engine refuses an existing id rather than overwrite a post
  you may have scheduled.
- **Check the sheet, not the folder.** One look at `_sheet.jpg` catches a bad set; flagged
  slides are named in the report.
- **Never post without `verify.py`.** Exit 1 on any byte-identical pair or any metadata. Its
  near-duplicate note (64-bit ahash ≤ 2 across posts) is a review signal, not a fail.

Thresholds and why each one exists: `references/thresholds.md`.

## Commands

| Command | What |
|---|---|
| `slideshow.py check` | script + pool errors (exit 1) and warnings; no font needed |
| `slideshow.py probe` | placement odds per slide on `--sample` (20) pool photos; renders nothing |
| `slideshow.py render` | one carousel; `--seed N` reproduces a render, `--audition N` photos per slide |
| `slideshow.py sheet DIR` | rebuild a preview sheet |
| `verify.py DIR…` | uniqueness + metadata strip across every post |
| `saliency.py PHOTO --out mask.png` | debug: see the forbidden map of one photo |

## Cost

Free. No API, no key, no network. Time: ~6–11 s per carousel on synthetic test photos (Apple
silicon); real photos with faces and clutter take longer, and each probe photo ~0.7 s.

## Limits

- **Best on macOS.** Faces, people, printed text and objects come from Apple Vision (pyobjc).
  Elsewhere (or `SLIDES_NO_VISION=1`) only edges and skin colour gate — check every slide.
- Handwriting, soft subject edges and low-contrast objects can slip past the gates.
- Portrait keeps clear of the top tabs and the bottom caption zone, but not the right-side
  action rail; keep heads short or check the right edge on the sheet.
- Inks are white, soft yellow and black only. No brand colours, outlines or text boxes.
- No posting, scheduling or captions writing: copy is yours, posting is another tool.
