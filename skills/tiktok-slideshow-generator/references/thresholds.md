# Thresholds and why they are set where they are

All constants live at the top of `scripts/saliency.py` and `scripts/slideshow.py`.

## Gates (a text block may touch zero forbidden cells)

| Constant | Value | Why |
|---|---|---|
| `GATE_EDGE` | 45 | per-pixel gradient that means an object line, not a fabric fold |
| `GATE_EDGE_COVER` | 95 | cover hook is big; folds and seams pass, a doorframe does not |
| `GATE_PAD` | 0.012 of width | clearance around everything forbidden; text touching a face reads as an accident |
| `GATE_CELL` | 0.35 | share of a 30 px cell that must be forbidden before it blocks |
| `GATE_OBJ_MAX` | 0.55 | a labelled object box bigger than this is the subject, not a prop |
| `SKIN_TOLERANCE` | 14 | chroma distance from this person's own face that still counts as skin |
| `INK_SPREAD_K` | 1.3 | ink must survive mean ± 1.3σ; row means hid mottled surfaces |

## Inks (lum, floor = minimum gap to the ground)

| Ink | Lum | Floor |
|---|---|---|
| white | 252 | 130 |
| yellow `#FFF4B3` | 241 | 78 |
| black | 34 | 62 |

Cover ink is judged on pixel percentiles (p10/p90), floor 120 (yellow 78), dimmed toward a 110
gap. Row means overstate cover contrast, so covers use pixels.

## Dim

`out = in·(1 − k·(in/255)²)` darkens highlights, leaves shadows. k is solved in closed form per
slide, the post takes the max, rounded up to 0.05. Cap 1/3 (beyond it photos go muddy). Floor
0.18 with a light ink: "only as needed" is often 0, and then the type only finds dark gaps.

## Search

| Constant | Value | Why |
|---|---|---|
| `SLIDE_AUDITION` | 14 | flagged = slides × (1 − p)^14 |
| `RETRY_MAX` | 3 | re-searches with the weak spot blocked before walking on |
| `SPLIT_TRIGGER` | 105 | split only the most awkward slides |
| `DUP_DIST` | 8 / 256 | dhash: a re-saved copy lands under it, different photos well over |

## Body length

A longer body finds a legal spot on fewer photos. When the full block fits nowhere, the head or
the body alone often still fits — that is why split exists.
