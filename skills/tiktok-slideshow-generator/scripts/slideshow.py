#!/usr/bin/env python3
"""TikTok slideshow engine: slide text + a folder of photos in, finished PNGs out.

    slideshow.py check  SCRIPT --covers DIR --backgrounds DIR --out DIR
    slideshow.py probe  SCRIPT --backgrounds DIR --font FONT [--sample 20]
    slideshow.py render SCRIPT --covers DIR --backgrounds DIR --font FONT --out DIR
    slideshow.py sheet  OUTPUT_DIR

The engine picks the photo, the crop, the ink, the dim, the size, the column and every
coordinate. You write the words. Placement is a search against Apple Vision's view of each
photo (faces, people, printed text, objects, where the eye goes) with HARD GATES — a spot on
skin, a face, printed words, an object line, a boundary between two surfaces, or a surface the
ink can't be read on is refused, never merely charged. When a slide has no legal spot it
shrinks, then splits head from body, then walks to the next photo; only the last photo may
take an ungated spot, and the report names that slide.

Covers are CONSUMED (moved to <covers>/used/ after a successful render). Backgrounds are
reused across carousels but never twice inside one. Output ids are never overwritten.
"""
import argparse
import hashlib
import json
import math
import random
import secrets
import shutil
import sys
import time
from functools import lru_cache
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageEnhance, ImageFont

import saliency as sal

IMG_EXT = {".jpg", ".jpeg", ".png", ".webp", ".heic"}

# ---------------------------------------------------------------- frames
# portrait = TikTok photo mode's native full screen. landscape = a 4:3 card the feed letterboxes
# onto black at ~1/3 screen height, so its type is set much bigger relative to the frame.
# portrait safe_bot 0.82: TikTok's caption and sound bar sit over the bottom of a photo post.
FRAMES = {
    "portrait": dict(W=1080, H=1920, margin=92, body=44, title=82, safe_bot=0.82,
                     block_max_h=0.72, head_w=1.0, head_w_max=1.0, lines_free=9, line_penalty=2.0,
                     widths=(0.42, 0.52, 0.62, 0.72, 0.80), weights=(0.6, 1.4, 2.4, 2.6, 1.6),
                     cover_widths=(0.46, 0.56, 0.66, 0.76, 0.86),
                     shrink=(1.0, 0.92, 0.84, 0.76, 0.70), grow=(1.15, 1.0, 0.88, 0.78, 0.70)),
    "landscape": dict(W=1920, H=1440, margin=300, body=78, title=107, safe_bot=0.93,
                      block_max_h=0.62, head_w=0.72, head_w_max=0.36, lines_free=6, line_penalty=9.0,
                      widths=(0.26, 0.32, 0.38, 0.44, 0.50, 0.56), weights=(0.5, 1.6, 2.4, 2.6, 2.0, 1.4),
                      cover_widths=(0.40, 0.52, 0.64, 0.76, 0.86),
                      shrink=(1.0, 0.92, 0.84, 0.76), grow=(1.45, 1.30, 1.15, 1.00, 0.88, 0.78, 0.70)),
}
F = FRAMES["portrait"]
W, H = F["W"], F["H"]


def set_frame(name):
    global F, W, H
    F = FRAMES[name]
    W, H = F["W"], F["H"]
    sal.configure(W, H)          # cells stay 30 px — every gate threshold is set for that size


# ---------------------------------------------------------------- inks
# (rgb, luminance, legibility floor). each ink has its own floor: pale yellow sits close to
# white in luminance, so yellow is a DARK-surface ink and white needs a big gap.
INKS = {"white": ((252, 251, 248), 252.0, 130.0),
        "yellow": ((255, 244, 179), 241.0, 78.0),     # TikTok's creamy caption yellow
        "black": ((38, 33, 30), 34.0, 62.0)}
# head/body pairs, auditioned in seeded order; the first that leaves no slide homeless wins.
# two tones place better than one: two colours can use two clean patches.
DEFAULT_PAIRS = [("white", "yellow"), ("yellow", "white"), ("white", "white"), ("black", "black")]

# ---------------------------------------------------------------- constants
BODY_GROW, HEAD_RATIO = 1.15, 1.28     # head is a size step over the body
HEAD_LEAD, BODY_LEAD = 1.3, 1.5
SAFE_TOP = 0.10
CHROME_FLOOR = 0.115                   # status bar + Following/For You tabs
COVER_BOT = 0.88
LINE_GAP = 0.42                        # head-to-body gap, share of body size
HEAD_MIN_WPL, HEAD_SQUEEZE = 3.0, 40.0  # a head two words to a row reads as broken
WIDTH_PULL, SHRINK_PENALTY = 180.0, 40.0
BUSY_NARROW = 0.22                     # forbidden share above which a photo counts as crowded
SPLIT_TRIGGER = 105.0                  # split only the awkward ones
SPLIT_MAX_V, SPLIT_MAX_H, SPLIT_SIDE_V = 0.20, 0.26, 0.14   # HARD caps: halves never drift apart
SPLIT_GAP, SPLIT_JOIN, SPLIT_BODY_CLEAR = 0.035, 70.0, 0.8
EDGE_KEEP, COLUMN_PULL, AWAY_PULL, PROFILE_WEIGHT = 70, 60.0, 90.0, 0.35
CLEAR_WANT, CLEAR_PULL = 5.0, 130.0    # cells of clearance wanted: middle of a pillow, not a gap
INK_WEIGHT, INK_HARD = 6.0, 90.0       # INK_HARD = ~15 lum short of the floor: refused
STEP_MAX, TEXTURE_MAX = 17.0, 9.0      # just above what reviewed-good blocks reach
RETRY_MAX = 3
# the dim: out = in * (1 - k*(in/255)^2). spent on highlights, shadows keep their depth. above
# k = 1/3 the curve turns over (a blown highlight goes grey), so that is the ceiling.
DIM_ROLLOFF = 2.0
GRADE_MAX = 1.0 / (DIM_ROLLOFF + 1.0)
GRADE_MIN = 0.18                       # "only as needed" is often 0 and leaves only dark gaps
GRADE_TARGET, GRADE_STEP = 78, 0.05
COVER_GRADE_TARGET = 110               # the hook buys a bigger gap than a slide
COVER_INK_FLOOR = 120.0                # on PIXEL percentiles: row means overstate cover contrast
COVER_SPAN_PCT = 10
COVER_AUDITION, COVER_QUIET, PHOTO_REJECT = 9, 58.0, 520.0
SUBJECT_MIN, EMPTY_FRAME_COST = 0.35, 220.0
CROP_ZOOMS, CROP_PANS = (1.0, 1.06, 1.13, 1.21), 7   # deeper zooms cut a letterbox sliver
CROP_TOL, CROP_SAME = 12.0, 0.03       # near-tie framings are drawn from, not argmin'd
SLIDE_AUDITION = 14                    # flagged = slides * (1 - p) ** audition
BG_NO_FIELD, BG_INK_MISS, BG_TIGHT, BG_FRESH_PULL = 1e4, 5e3, 90.0, 12.0
DUP_DIST = 8                           # dhash/256: a re-saved copy lands under it, different photos well over

FONT = {"path": None, "weight": 700, "emoji": None}


# ---------------------------------------------------------------- type

@lru_cache(maxsize=256)
def font(size):
    f = ImageFont.truetype(FONT["path"], max(12, int(size)))
    try:                                 # variable font: set its weight axis, leave the rest
        axes = f.get_variation_axes()
        vals = [FONT["weight"] if b"eight" in (a.get("name") or b"") or a.get("name") == "Weight"
                else a["default"] for a in axes]
        f.set_variation_by_axes(vals)
    except Exception:
        pass
    return f


def is_emoji(ch):
    o = ord(ch)
    return (0x1F000 <= o <= 0x1FAFF or 0x2600 <= o <= 0x27BF or 0x2B00 <= o <= 0x2BFF
            or o == 0x200D or 0xFE00 <= o <= 0xFE0F or o == 0x20E3)


def runs(text):
    out = []
    for ch in text:
        e = bool(FONT["emoji"]) and is_emoji(ch)
        if out and out[-1][0] == e:
            out[-1][1] += ch
        else:
            out.append([e, ch])
    return out


@lru_cache(maxsize=512)
def emoji_tile(s, px):
    for strike in (160, 109, 96, 64):    # colour emoji fonts load only at their bitmap strikes
        try:
            ef = ImageFont.truetype(FONT["emoji"], strike)
            c = Image.new("RGBA", (strike * 3 * max(1, len(s)), strike * 2), (0, 0, 0, 0))
            ImageDraw.Draw(c).text((strike // 2, strike // 2), s, font=ef, embedded_color=True)
            t = c.crop(c.getbbox() or (0, 0, 1, 1))
            return t.resize((max(1, round(t.width * px / t.height)), px), Image.LANCZOS)
        except Exception:
            continue
    return Image.new("RGBA", (1, px), (0, 0, 0, 0))


def tlen(text, f):
    return sum(emoji_tile(s, round(f.size * 0.9)).width if e else f.getlength(s)
               for e, s in runs(text))


def wrap(text, f, max_w):
    lines = []
    for raw in str(text).split("\n"):
        cur = ""
        for w in raw.split():
            trial = (cur + " " + w).strip()
            if tlen(trial, f) <= max_w or not cur:
                cur = trial
            else:
                lines.append(cur)
                cur = w
        lines.append(cur)
    return lines


def fit_lines(text, base, grown, max_w, min_size):
    """largest size (grown -> base) at which every line the AUTHOR wrote stays one line;
    else wrap at base, stepping down until no line ends on an orphan word."""
    paras = [p.strip() for p in str(text).split("\n") if p.strip()]
    s = grown
    while s >= min(base, grown) and paras:
        if all(tlen(p, font(s)) <= max_w for p in paras):
            return paras, font(s)
        s -= 2
    s = min(base, grown)
    while True:
        lines = wrap(text, font(s), max_w)
        if len(lines) == 1 or len(lines[-1].split()) >= 2 or s <= min_size:
            return lines, font(s)
        s -= 4


def rows_h(rows):
    return sum(int(r[1].size * r[-1]) for r in rows)


def words_per_line(rows):
    return sum(len(r[0].split()) for r in rows) / max(1, len(rows))


def draw_rows(img, rows, x, y, w, align, fills):
    d = ImageDraw.Draw(img)
    for (text, f, ld), fill in zip(rows, fills):
        lx = x + ((w - tlen(text, f)) / 2 if align == "center" else 0)
        for e, s in runs(text):
            if e:
                t = emoji_tile(s, round(f.size * 0.9))
                img.alpha_composite(t, (round(lx), round(y + f.size * 0.2)))
                lx += t.width
            else:
                d.text((lx, y), s, font=f, fill=fill)
                lx += f.getlength(s)
        y += int(f.size * ld)


# ---------------------------------------------------------------- light

def dim_scale(lum, k):
    k = min(max(float(k), 0.0), GRADE_MAX)
    return 1.0 - k * (np.clip(np.asarray(lum, np.float64), 0, 255) / 255.0) ** DIM_ROLLOFF


def dim_grid(lg, k):
    lg = np.asarray(lg, np.float64)
    return lg * dim_scale(lg, k)


def dim_image(img, k):
    if k <= 0.002:
        return img.convert("RGBA")
    a = np.asarray(img.convert("RGB"), np.float32)
    lum = a @ np.array([0.299, 0.587, 0.114], np.float32)
    return Image.fromarray(np.clip(a * dim_scale(lum, k).astype(np.float32)[..., None], 0, 255)
                           .astype(np.uint8)).convert("RGBA")


def lum_px(img, y0, y1, x0, x1):
    """24x8 luminance samples of the strip the type actually sits on — its own x range."""
    y0, y1 = max(0, int(y0)), min(H, int(y1))
    x0, x1 = sorted((max(0, int(x0)), min(W, int(x1))))
    if x1 - x0 < 8:
        x0, x1 = max(0, x0 - 8), min(W, x1 + 8)
    if y1 <= y0:
        return np.full(1, 128.0)
    return np.asarray(img.convert("L").crop((x0, y0, x1, y1)).resize((24, 8)), np.float64).ravel()


def grade_need(img, rect, ink, target=GRADE_TARGET, cap=GRADE_MAX):
    """dim k that lands this block's mean at ink - target. closed form: the curve's mean is
    linear in k (M1 - k*Mp). the old (L-T)/L arithmetic made the dim all-or-nothing."""
    il = INKS[ink][1]
    if il < 128:
        return 0.0                       # darkening only ever hurts a dark ink
    lum = lum_px(img, *rect)
    want, m1 = il - target, float(lum.mean())
    mp = float((lum ** (DIM_ROLLOFF + 1)).mean()) / 255.0 ** DIM_ROLLOFF
    return 0.0 if m1 <= want or mp <= 1e-6 else float(min(cap, (m1 - want) / mp))


def sep_of(ink, lo, hi):
    il = INKS[ink][1]
    return min(abs(il - lo), abs(il - hi))


def rows_weak(img, rows, x, y, w, ink):
    """worst-row separation under the floor? a tall block's mean says bright while its last
    line sits on dark hair, so each row's strip is measured."""
    lums, yy = [], y
    for r in rows:
        step = int(r[1].size * r[-1])
        lums.append(float(lum_px(img, yy, yy + step, x, x + w).mean()))
        yy += step
    return bool(lums) and sep_of(ink, min(lums), max(lums)) < INKS[ink][2]


def regrade(img, rng):
    """invisible colour shift so two renders of one photo don't share a colour fingerprint:
    temperature push AWAY from the existing cast, small hue turn, contrast, saturation."""
    px = np.asarray(img.resize((32, 32)), np.float32)
    warm = float((px[..., 0] - px[..., 2]).mean() / 255.0)
    t = max(-0.055, min(0.055, -warm * 0.8)) + rng.uniform(-0.04, 0.04)
    a = np.asarray(img.convert("RGB"), np.float32)
    a[..., 0] *= 1 + t
    a[..., 2] *= 1 - t
    img = Image.fromarray(np.clip(a, 0, 255).astype(np.uint8))
    h, s, v = img.convert("HSV").split()
    dh = rng.randint(-11, 11)
    img = Image.merge("HSV", (h.point(lambda x: (x + dh) % 256), s, v)).convert("RGB")
    img = ImageEnhance.Contrast(img).enhance(rng.uniform(0.93, 1.08))
    return ImageEnhance.Color(img).enhance(rng.uniform(0.90, max(0.98, 1.10 - max(0.0, warm) * 1.2)))


def finish(img, rng):
    """last per-render jitter, then a fresh pixel-only image: no EXIF, no text chunks, no ICC."""
    img = ImageEnhance.Brightness(img.convert("RGB")).enhance(rng.uniform(0.975, 1.025))
    img = ImageEnhance.Color(img).enhance(rng.uniform(0.97, 1.03))
    return Image.fromarray(np.asarray(img))


# ---------------------------------------------------------------- pools

def readable(p):
    try:
        with Image.open(p) as im:
            im.verify()
        return True
    except Exception:
        print(f"  ! {p.name} is not a readable image — skipped")
        return False


def list_images(folder):
    d = Path(folder)
    if not d.is_dir():
        return []
    return [p for p in sorted(d.iterdir()) if p.suffix.lower() in IMG_EXT
            and not p.name.startswith(".") and readable(p)]


def load(p):
    im = Image.open(p)
    im.draft("RGB", (2400, 2400))        # JPEG decodes at reduced size; output is <= 1920
    return im.convert("RGB")


def dhash(p, size=16):
    g = np.asarray(Image.open(p).convert("L").resize((size + 1, size), Image.LANCZOS), np.int16)
    return int("".join("1" if b else "0" for b in (g[:, :-1] < g[:, 1:]).ravel()), 2)


def sha256(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def fresh_covers(folder):
    """cover pool minus re-downloads of covers already used (byte hash or dhash)."""
    used = [(sha256(q), dhash(q)) for q in list_images(Path(folder) / "used")]
    out = []
    for p in list_images(folder):
        s, d = sha256(p), dhash(p)
        if any(s == us or bin(d ^ ud).count("1") <= DUP_DIST for us, ud in used):
            print(f"  ! {p.name} is the same picture as a used cover — skipped, left in place")
        else:
            out.append(p)
    return out


def avg_color(p):
    im = Image.open(p)
    im.draft("RGB", (64, 64))
    return np.asarray(im.convert("RGB").resize((32, 32)), np.float32).reshape(-1, 3).mean(0)


def color_pick(rng, paths, n):
    """a random anchor + its n-1 nearest by average colour: one mood per carousel."""
    if len(paths) <= n:                 # small pool: shuffle, or slide k always gets photo k
        return rng.sample(paths, len(paths))
    cols = {p: avg_color(p) for p in paths}
    anchor = rng.choice(paths)
    rest = sorted((p for p in paths if p != anchor), key=lambda p: float(((cols[p] - cols[anchor]) ** 2).sum()))
    return [anchor] + rest[:n - 1]


def use_counts(out_root):
    """how many shipped slides each background is already behind. the renders ARE the ledger."""
    counts = {}
    for m in Path(out_root).glob("*/meta.json"):
        try:
            for s in json.loads(m.read_text())["slides"]:
                counts[Path(s["photo"]).name] = counts.get(Path(s["photo"]).name, 0) + 1
        except Exception:
            continue
    return counts


# ---------------------------------------------------------------- crop

def plan_crop(img, key, band, salt, rect=None):
    """choose the window in SOURCE coordinates, then cut and regrade it.
    returns (frame, grid, rect, score). near-tie windows are drawn by hash, so a photo that
    comes round again gets a different, equally good framing."""
    src = sal.analyze_source(img, key)
    sw, sh = img.size
    cands = [tuple(rect)] if rect else []
    for z in ([] if rect else CROP_ZOOMS):
        if sw / sh >= W / H:
            wh = 1.0 / z
            ww = wh * (sh / sw) * (W / H)
        else:
            ww = 1.0 / z
            wh = ww * (sw / sh) / (W / H)
        if ww > 1.0 or wh > 1.0:
            continue
        for i in range(CROP_PANS):
            for j in range(CROP_PANS):
                x0, y0 = (1 - ww) * i / (CROP_PANS - 1), (1 - wh) * j / (CROP_PANS - 1)
                cands.append((x0, y0, x0 + ww, y0 + wh))
    scored = []
    for r in cands:
        g = sal.window_grid(src, r)
        s = sal.window_penalty(src, r) + sal.band_cost(g, band[0], band[1], H)
        if sal.subject_mass(g) < SUBJECT_MIN:
            s += EMPTY_FRAME_COST                # empty wall: quietest band, dead slide
        scored.append((s, r, g))
    best = min(scored, key=lambda t: t[0])
    if not rect:
        keep = []
        for t in sorted((t for t in scored if t[0] <= best[0] + CROP_TOL), key=lambda t: t[0]):
            if all(max(abs(a - b) for a, b in zip(t[1], k[1])) > CROP_SAME for k in keep):
                keep.append(t)
        best = keep[int(hashlib.md5(f"{salt}|{key}".encode()).hexdigest()[:8], 16) % len(keep)]
    score, r, g = best
    box = (int(r[0] * sw), int(r[1] * sh), int(r[2] * sw), int(r[3] * sh))
    frame = regrade(img.crop(box).resize((W, H), Image.LANCZOS), random.Random(f"{salt}|rg|{key}"))
    return frame, g, [round(v, 4) for v in r], score


# ---------------------------------------------------------------- placement

def analysis(frame, key, relax=False, avoid=()):
    g = sal.gate(frame, key=key, relax=relax)
    if avoid:                            # spots a previous attempt proved unreadable
        g = g.copy()
        for x0, y0, x1, y1 in avoid:
            g[max(0, int(y0 / H * sal.GH)):int(y1 / H * sal.GH) + 1,
              max(0, int(x0 / W * sal.GW)):int(x1 / W * sal.GW) + 1] = 1.0
    return {"prof": sal.profile(frame, key), "lum": sal.lum_grid(frame), "gate": g,
            "clear": sal.clear_grid(g), "surf": sal.surface_grid(frame, key),
            "away": sal.subject_side(frame, key)}


def place(ctx, total, box_w, col, top, bot, ink=None, ink2=None, head_h=None, grade=GRADE_MAX,
          gated=True, min_clear=None):
    """best (y, x, cost) for a box_w x total block, or None if no LEGAL position exists."""
    prof = ctx["prof"]
    gh, gw = prof.shape
    cw, ch = max(1, round(box_w / W * gw)), max(1, round(total / H * gh))
    if cw > gw or ch > gh:
        return None
    costs = sal.cost_map(prof, cw, ch) * PROFILE_WEIGHT
    pen = None
    if ink:
        eff = dim_grid(ctx["lum"], grade)    # judged on the frame that SHIPS, i.e. dimmed
        hc = 0 if not head_h or ch < 2 else max(1, min(round(head_h / H * gh), ch - 1))
        if ink2 and ink2 != ink and hc:      # two tones = two strips, two legibility questions
            ph = sal.ink_cost_map(eff, cw, hc, INKS[ink][1], INKS[ink][2], INK_WEIGHT)
            pb = sal.ink_cost_map(eff, cw, ch - hc, INKS[ink2][1], INKS[ink2][2], INK_WEIGHT)
            pen = ph[:costs.shape[0]] + pb[hc:hc + costs.shape[0]]
        else:
            pen = sal.ink_cost_map(eff, cw, ch, INKS[ink][1], INKS[ink][2], INK_WEIGHT)
        costs = costs + pen
    if gated:
        costs[sal.gate_map(ctx["gate"], cw, ch)] = np.inf
        clear = sal.clear_map(ctx["clear"], cw, ch)
        costs = costs + np.maximum(0.0, CLEAR_WANT - clear) * CLEAR_PULL
        if min_clear is not None:
            costs[clear < min_clear] = np.inf
        step, tex = sal.surface_map(ctx["surf"], cw, ch)
        costs[(step > STEP_MAX) | (tex > TEXTURE_MAX)] = np.inf
        if pen is not None:
            costs[pen > INK_HARD] = np.inf
    rows, cols = costs.shape
    r_lo, r_hi = int(top / H * gh), max(int(top / H * gh), int((bot - total) / H * gh))
    costs[:r_lo] = np.inf
    costs[r_hi + 1:] = np.inf
    edge = max(1, int(EDGE_KEEP / W * gw))
    costs[:, :edge] = np.inf
    costs[:, max(0, cols - edge):] = np.inf
    if not np.isfinite(costs).any():
        return None
    xs = np.arange(cols)[None, :]
    want = {"left": 0.0, "center": (cols - 1) / 2, "right": float(cols - 1)}[col]
    costs = costs + np.abs(xs - want) / max(cols, 1) * COLUMN_PULL
    if ctx["away"]:                          # push off the half the eye already goes to
        mid = (cols - 1) / 2
        over = (mid - xs) if ctx["away"] == "left" else (xs - mid)
        costs = costs + np.clip(over, 0, None) / max(cols, 1) * AWAY_PULL
    r, c = np.unravel_index(np.argmin(costs), costs.shape)
    return int(r / gh * H), int(c / gw * W), float(costs[r, c])


def layout_slide(frame, head, body, key, inks, grade, rng, fallback, avoid=()):
    """head+body block on one framed photo. returns a plan or None (no legal spot)."""
    ctx = analysis(frame, key, avoid=avoid)
    size = F["body"] * BODY_GROW
    top, bot = int(H * SAFE_TOP), int(H * F["safe_bot"])
    cap = min(bot - top, int(H * F["block_max_h"]))
    widths, cols = F["widths"], ("left", "center", "right")
    crowded = float((ctx["gate"] > sal.GATE_CELL).mean()) > BUSY_NARROW
    want = rng.choices(widths, weights=F["weights"][::-1] if crowded else F["weights"])[0]
    has_h, has_b = bool(str(head).strip()), bool(str(body).strip())

    def lay(frac, shrink, with_head=True, with_body=True):
        bw = int(W * frac)
        hf, bf = font(size * HEAD_RATIO * shrink), font(size * shrink)
        hr = [(ln, hf, HEAD_LEAD) for ln in wrap(head, hf, min(bw * F["head_w"], W * F["head_w_max"]))] \
            if has_h and with_head else []
        br = [(ln, bf, BODY_LEAD) for ln in wrap(body, bf, bw)] if has_b and with_body else []
        g = int(bf.size * LINE_GAP) if hr and br else 0
        return hr, br, g, rows_h(hr + br) + g, bw

    def fit(frac, shrink, **kw):         # step down together until the block fits the cap
        s = shrink
        while True:
            laid = lay(frac, s, **kw)
            if laid[3] <= cap or s <= F["shrink"][-1]:
                return (laid, s) if laid[3] <= cap else (None, s)
            s -= 0.04

    best = None
    for step in F["shrink"]:
        for frac in widths:
            laid, s = fit(frac, step)
            if laid is None:
                continue
            hr, br, g, tot, bw = laid
            for col in cols:
                r = place(ctx, tot, bw, col, top, bot, inks[0], inks[1], rows_h(hr) + g, grade)
                if r is None:
                    continue
                cost = r[2] + max(0, len(hr) + len(br) - F["lines_free"]) * F["line_penalty"]
                cost += abs(frac - want) * WIDTH_PULL + (1.0 - step) * SHRINK_PENALTY
                if hr and words_per_line(hr) < HEAD_MIN_WPL:
                    cost += (HEAD_MIN_WPL - words_per_line(hr)) * HEAD_SQUEEZE
                if best is None or cost < best[0]:
                    best = (cost, [{"rows": hr + br, "n_head": len(hr), "gap": g, "x": r[1],
                                    "y": r[0], "w": bw}], s)
        if best:
            break

    # THE SPLIT IS THE RESCUE: a block holding head+body has a legal home on far fewer photos
    # than the head or the body alone. claim in one patch, reason in another,
    # UNDER it or BESIDE it — never the opposite corner.
    split = None
    if has_h and has_b and (best is None or best[0] > SPLIT_TRIGGER):
        for hfr in widths[:4]:
            hr = lay(hfr, 1.0, with_body=False)[0]
            h_bw = max(int(W * hfr), int(max(tlen(r[0], r[1]) for r in hr)))  # real drawn width
            h_tot = rows_h(hr)
            if h_tot > cap * 0.5:
                continue
            hp = place(ctx, h_tot, h_bw, cols[0], top, bot, inks[0], None, None, grade)
            if hp is None:
                continue
            hy, hx, hcost = hp
            for bfr in widths[:4]:
                laid, _s = fit(bfr, 1.0, with_head=False)
                if laid is None or h_tot + laid[3] > cap:
                    continue
                br, b_tot, b_bw = laid[1], laid[3], laid[4]
                found = None
                # under: below the head, within SPLIT_MAX_V / SPLIT_MAX_H
                lo = hy + h_tot + int(H * SPLIT_GAP)
                bp = place(ctx, b_tot, b_bw, cols[-1], lo, min(bot, lo + int(H * SPLIT_MAX_V) + b_tot),
                           inks[1], None, None, grade, min_clear=CLEAR_WANT * SPLIT_BODY_CLEAR)
                if bp and abs(bp[1] - hx) <= W * SPLIT_MAX_H:
                    found = bp
                # beside: right of the head, tops within SPLIT_SIDE_V
                if found is None and hx + h_bw + W * SPLIT_GAP + b_bw <= W:
                    sub = dict(ctx, gate=ctx["gate"].copy())
                    sub["gate"][:, :min(sal.GW, int((hx + h_bw + W * SPLIT_GAP) / W * sal.GW) + 1)] = 1.0
                    bp = place(sub, b_tot, b_bw, "right", max(top, hy - int(H * SPLIT_SIDE_V)),
                               min(bot, hy + int(H * SPLIT_SIDE_V) + b_tot), inks[1], None, None, grade,
                               min_clear=CLEAR_WANT * SPLIT_BODY_CLEAR)
                    if bp and abs(bp[0] - hy) <= H * SPLIT_SIDE_V:
                        found = bp
                if found is None:
                    continue
                by, bx, bcost = found
                # weighted MEAN by height: a sum compared two placements against one and lost always
                cost = (hcost * h_tot + bcost * b_tot) / max(1, h_tot + b_tot) \
                    + (abs(by - hy) / H + abs(bx - hx) / W) * SPLIT_JOIN
                if split is None or cost < split[0]:
                    split = (cost, [{"rows": hr, "n_head": len(hr), "gap": 0, "x": hx, "y": hy, "w": h_bw},
                                    {"rows": br, "n_head": 0, "gap": 0, "x": bx, "y": by, "w": b_bw}], 1.0)

    status = "one"
    if split and (best is None or split[0] < best[0]):
        best, status = split, "split"
    if best is None:
        if not fallback:
            return None              # a gate with an ungated fallback is not a gate: next photo
        status = "fallback"
        for frac in widths:
            laid, s = fit(frac, F["shrink"][-1])
            if laid is None:
                continue
            hr, br, g, tot, bw = laid
            for col in cols:
                r = place(ctx, tot, bw, col, top, bot, inks[0], inks[1], rows_h(hr) + g, grade, gated=False)
                if r and (best is None or r[2] < best[0]):
                    best = (r[2], [{"rows": hr + br, "n_head": len(hr), "gap": g, "x": r[1], "y": r[0], "w": bw}], s)
        if best is None:
            hr, br, g, tot, bw = lay(widths[-1], F["shrink"][-1])
            best = (0.0, [{"rows": hr + br, "n_head": len(hr), "gap": g, "x": (W - bw) // 2, "y": top, "w": bw}],
                    F["shrink"][-1])
    return {"parts": best[1], "status": status, "shrink": round(best[2], 2)}


def tiers(pt):
    """(rows, y) runs that share one ink — head rows, then body rows below the gap."""
    nh, rows = pt["n_head"], pt["rows"]
    out = []
    if nh:
        out.append((rows[:nh], pt["y"], True))
    if nh < len(rows):
        out.append((rows[nh:], pt["y"] + rows_h(rows[:nh]) + (pt["gap"] if nh else 0), False))
    return out


def rects(plan):
    return [(pt["x"], pt["y"], pt["x"] + pt["w"], pt["y"] + rows_h(pt["rows"]) + pt["gap"])
            for pt in plan["parts"]]


def plan_slide(frame, head, body, key, inks, grade, rng_seed, fallback):
    """layout + the legibility check on the DIMMED frame, with up to RETRY_MAX re-placements.
    a block still weak after that sends the walker to the next photo (or ships flagged)."""
    avoid, shown = [], dim_image(frame, grade)
    for _attempt in range(RETRY_MAX + 1):
        plan = layout_slide(frame, head, body, key, inks, grade, random.Random(rng_seed), fallback, avoid)
        if plan is None:
            return None
        need = max([grade_need(frame, (y, y + rows_h(r), pt["x"], pt["x"] + pt["w"]), inks[0 if ih else 1])
                    for pt in plan["parts"] for r, y, ih in tiers(pt)] or [0.0])
        weak = any(rows_weak(shown, r, pt["x"], y, pt["w"], inks[0 if ih else 1])
                   for pt in plan["parts"] for r, y, ih in tiers(pt))
        plan.update(need=need, weak=weak)
        if not weak:
            return plan
        avoid += rects(plan)
    if not fallback:
        return None
    plan["status"] = "weak"
    return plan


# ---------------------------------------------------------------- cover

def cover_rows(lines, frac, grow):
    bw, t = int(W * frac), F["title"]
    tl, tf = fit_lines(lines[0], t, t * grow, bw, t * 0.62)
    rows = [(ln, tf, 1.24) for ln in tl]
    gap = 0
    if len(lines) > 1 and str(lines[1]).strip():
        sl, sf = fit_lines(lines[1], t * 0.71, t * 0.71 * grow, bw, t * 0.52)
        gap = int(t * 0.41)
        rows += [(ln, sf, 1.3) for ln in sl]
        return rows, len(tl), gap, rows_h(rows) + gap, bw
    return rows, len(tl), 0, rows_h(rows), bw


def calm(grid, total, widths):
    """least-bad UNGATED cost of a block this tall anywhere on a framed photo — the score the
    photo auditions rank on. it barely correlates with the crop-window score."""
    ctx = {"prof": grid, "away": None}
    fits = [place(ctx, total, int(W * fr), c, int(H * CHROME_FLOOR), int(H * cover_bot()), gated=False)
            for fr in widths for c in ("left", "center", "right")]
    return min((r[2] for r in fits if r), default=PHOTO_REJECT * 2)


def cover_bot():
    return min(COVER_BOT, F["safe_bot"])


def layout_cover(frame, lines, key, ink):
    """same width/column/y search a slide gets, with the RELAXED gate, biggest type first.
    an orphan word on its own row is refused, not charged (except at the widest rung)."""
    ctx = analysis(frame, key, relax=True)
    top, bot = int(H * CHROME_FLOOR), int(H * cover_bot())
    widths = F["cover_widths"]
    for gated in (True, False):
        best = None
        for grow in (F["grow"] if gated else F["grow"][-1:]):
            for frac in widths:
                rows, nt, gap, tot, bw = cover_rows(lines, frac, grow)
                if tot > bot - top:
                    continue
                if gated and frac != widths[-1] and any(len(r[0].split()) == 1 and not r[0].strip().isdigit()
                                                        for r in rows):
                    continue
                for col in ("left", "center", "right"):
                    r = place(ctx, tot, bw, col, top, bot, ink, None, None, GRADE_MAX, gated=gated)
                    if r is None:
                        continue
                    cost = r[2] + max(0, len(rows) - F["lines_free"]) * F["line_penalty"]
                    if words_per_line(rows) < HEAD_MIN_WPL:
                        cost += (HEAD_MIN_WPL - words_per_line(rows)) * HEAD_SQUEEZE * 2
                    if best is None or cost < best[0]:
                        best = (cost, {"rows": rows, "sub_at": nt,
                                       "sub_gap": gap, "x": r[1], "y": r[0], "w": bw})
            if best:
                return {"part": best[1], "status": "one" if gated else "fallback", "cost": best[0]}
    return None


def cover_y_rows(pt):
    """y of every row, honouring the title/subtitle gap."""
    ys, y = [], pt["y"]
    for i, r in enumerate(pt["rows"]):
        if i == pt["sub_at"] and pt["sub_gap"]:
            y += pt["sub_gap"]
        ys.append(y)
        y += int(r[1].size * r[-1])
    return ys


def finish_cover(frame, plan, ink):
    """grade for the hook's ink, judge on PIXEL percentiles; under the floor -> the ink with the
    best margin over its own floor, regraded for it."""
    pt = plan["part"]
    rect = (pt["y"], pt["y"] + rows_h(pt["rows"]) + pt["sub_gap"], pt["x"], pt["x"] + pt["w"])

    def try_ink(name):
        img = dim_image(frame, grade_need(frame, rect, name, COVER_GRADE_TARGET))
        px = lum_px(img, *rect)
        sep = sep_of(name, np.percentile(px, COVER_SPAN_PCT), np.percentile(px, 100 - COVER_SPAN_PCT))
        floor = INKS[name][2] if name == "yellow" else COVER_INK_FLOOR
        return sep - floor, img, sep

    margin, img, sep = try_ink(ink)
    if margin < 0:
        alt = max(((try_ink(n), n) for n in INKS if n != ink), key=lambda t: t[0][0])
        if alt[0][0] > margin:
            (margin, img, sep), ink = alt
    return img, ink, round(float(sep)), margin < 0


# ---------------------------------------------------------------- carousel

def bg_field_fee(frame, key, ink):
    """can this photo hold a block at all, and in this ink? only failing the FLOOR shape is a
    verdict; failing the roomy one is a queue fee (a hard test there would bury much of a pool)."""
    g = sal.gate(frame, key=key)
    lg = sal.lum_grid(frame)
    il, fl = INKS[ink][1], INKS[ink][2]
    if il >= 128:
        lg = dim_grid(lg, GRADE_MIN)
    body = F["body"] * BODY_GROW
    floor = (F["widths"][0], 2 * body * BODY_LEAD / H)
    roomy = (F["widths"][1], (body * HEAD_RATIO * HEAD_LEAD + 3 * body * BODY_LEAD) / H)

    def holds(shape):
        cw, ch = max(1, round(shape[0] * sal.GW)), max(1, round(shape[1] * sal.GH))
        legal = ~sal.gate_map(g, cw, ch)
        if not legal.any():
            return False, False
        return True, bool(sal.ink_cost_map(lg, cw, ch, il, fl, INK_WEIGHT)[legal].min() == 0)

    if all(holds(roomy)):
        return 0.0
    field, ink_ok = holds(floor)
    return BG_NO_FIELD if not field else (BG_INK_MISS if not ink_ok else BG_TIGHT)


class Carousel:
    def __init__(self, script, a):
        self.s, self.a = script, a
        self.seed = a.seed if a.seed is not None else secrets.randbelow(2 ** 31)
        self.rng = random.Random(self.seed)
        self.frames = {}
        self.log = []

    def say(self, msg):
        print(msg)
        self.log.append(msg)

    def frame(self, path, key, band):
        if key not in self.frames:
            self.frames[key] = plan_crop(load(path), key, band, self.seed)
        return self.frames[key]

    def pick_cover(self):
        s, a = self.s, self.a
        if s.get("cover_photo"):
            return Path(s["cover_photo"]), False
        pool = fresh_covers(a.covers)
        if not pool:
            sys.exit(f"no fresh cover photos in {a.covers} — add some (or move used/ back)")
        band = (int(H * 0.14), int(H * 0.14) + cover_rows(s["cover"], F["cover_widths"][-1], 1.0)[3])
        tried = []
        for p in self.rng.sample(pool, min(COVER_AUDITION, len(pool))):
            grid = self.frame(p, f"cov:{p.name}", band)[1]
            score = calm(grid, band[1] - band[0], F["cover_widths"])
            tried.append((score, p))
            if score <= COVER_QUIET:
                break
        score, best = min(tried, key=lambda t: t[0])
        if score > PHOTO_REJECT:
            self.say(f"  ! no clean cover photo (best {score:.0f} > {PHOTO_REJECT:.0f}) — add covers")
        return best, True

    def walk(self, i, slide, group, inks, grade):
        """try this slide's ranked candidates until one holds the copy. last may fall back."""
        cands = [Path(slide["photo"])] if slide.get("photo") else group
        band = (int(H * SAFE_TOP), int(H * 0.83))
        if not slide.get("photo"):
            fee = []
            for p in cands:
                key = f"bg:{p.name}"
                fr, grid, _r, _s = self.frame(p, key, band)
                sc = calm(grid, band[1] - band[0], F["cover_widths"]) + bg_field_fee(fr, key, inks[1])
                if sc < BG_INK_MISS:      # freshness fee: calmness alone must not decide every post
                    sc += self.counts.get(p.name, 0) * BG_FRESH_PULL
                fee.append((sc, p))
            cands = [p for _s, p in sorted(fee, key=lambda t: t[0])]
        for n, p in enumerate(cands):
            key = f"bg:{p.name}"
            fr, _g, rect, _s = self.frame(p, key, band)
            plan = plan_slide(fr, slide.get("head", ""), slide.get("body", ""), key, inks, grade,
                              f"{self.seed}|{i}|{p.name}", fallback=(n == len(cands) - 1))
            if plan:
                plan.update(photo=p, rect=rect, key=key)
                return plan
        return None

    def run(self, inks, grade):
        plans = [self.walk(i, sl, self.groups[i], inks, grade) for i, sl in enumerate(self.s["slides"])]
        cov = layout_cover(self.cover_frame, self.s["cover"], self.cover_key, inks[0])
        homeless = sum(p["status"] in ("fallback", "weak") for p in plans) + (cov["status"] != "one")
        return plans, cov, homeless

    def render(self):
        s, a = self.s, self.a
        t0 = time.time()
        cover_path, consume = self.pick_cover()
        self.cover_key = f"cov:{cover_path.name}"
        band = (int(H * 0.14), int(H * 0.14) + cover_rows(s["cover"], F["cover_widths"][-1], 1.0)[3])
        self.cover_frame, _g, cover_rect, _sc = self.frame(cover_path, self.cover_key, band)

        fixed = {Path(sl["photo"]).name for sl in s["slides"] if sl.get("photo")}
        pool = [p for p in list_images(a.backgrounds) if p.name not in fixed]
        free = [i for i, sl in enumerate(s["slides"]) if not sl.get("photo")]
        if len(pool) < len(free):
            sys.exit(f"{a.backgrounds} has {len(pool)} photos, this script needs {len(free)}")
        picked = color_pick(self.rng, pool, min(len(pool), len(free) * a.audition))
        self.groups = {i: picked[k::len(free)] for k, i in enumerate(free)}   # disjoint: no repeats
        self.counts = use_counts(a.out)

        pinned = a.inks.split("/") if a.inks else s.get("inks")
        pairs = [tuple(pinned)] if pinned else self.rng.sample(DEFAULT_PAIRS, len(DEFAULT_PAIRS))
        tried, best = [], None
        for inks in pairs:
            floor = GRADE_MIN if max(INKS[n][1] for n in inks) >= 128 else 0.0
            plans, cov, homeless = self.run(inks, floor)
            tried.append(f"{inks[0]}/{inks[1]}:{homeless}")
            if best is None or homeless < best[0]:
                best = (homeless, inks, floor, plans)
            if not homeless:
                break
        _h, inks, floor, plans = best
        grade = min(GRADE_MAX, max(floor, math.ceil(max(p["need"] for p in plans) / GRADE_STEP) * GRADE_STEP))
        plans, cov, _h = self.run(inks, grade)     # final pass at the carousel's ONE exposure
        self.say(f"  ink: {inks[0]} head / {inks[1]} body   (tried {', '.join(tried)})")
        self.say(f"  dim: {grade:.2f} on every slide")

        final = Path(a.out) / s["id"]
        out = Path(a.out) / f".{s['id']}.partial"
        shutil.rmtree(out, ignore_errors=True)
        out.mkdir(parents=True)
        rng = random.Random(f"{self.seed}|finish")
        img, cov_ink, sep, weak = finish_cover(self.cover_frame, cov, inks[0])
        pt = cov["part"]
        for (row, y) in zip(pt["rows"], cover_y_rows(pt)):
            draw_rows(img, [row], pt["x"], y, pt["w"], "center", [INKS[cov_ink][0]])
        finish(img, rng).save(out / "01.png")
        cflag = cov["status"] != "one" or weak
        self.say(f"  01 cover  {cover_path.name}  ink {cov_ink} sep {sep}"
                 + ("   ! FLAG: " + ("no clean field, least-bad spot" if cov["status"] != "one"
                                     else "low contrast") if cflag else ""))
        meta = {"id": s["id"], "seed": self.seed, "frame": a.frame, "inks": list(inks), "dim": grade,
                "cover": {"photo": str(cover_path), "rect": cover_rect, "ink": cov_ink, "sep": sep,
                          "status": "flag" if cflag else "ok"}, "slides": []}
        align = self.rng.choice(("left", "center"))
        for i, p in enumerate(plans, 2):
            img = dim_image(self.frames[p["key"]][0], grade)
            for part in p["parts"]:
                for rows, y, is_head in tiers(part):
                    draw_rows(img, rows, part["x"], y, part["w"], align,
                              [INKS[inks[0] if is_head else inks[1]][0]] * len(rows))
            finish(img, rng).save(out / f"{i:02d}.png")
            flag = p["status"] in ("fallback", "weak")
            self.say(f"  {i:02d} {p['status']:8} {p['photo'].name}  size x{p['shrink']}"
                     + ("   ! FLAG: " + ("no legal spot on any candidate" if p["status"] == "fallback"
                                         else "ink does not separate") if flag else ""))
            meta["slides"].append({"photo": str(p["photo"]), "rect": p["rect"], "status": p["status"],
                                   "shrink": p["shrink"]})
        n = len(plans) + 1
        flagged = [m for m in meta["slides"] if m["status"] in ("fallback", "weak")] + ([1] if cflag else [])
        self.say(f"  placement: {n - len(flagged)}/{n} clean"
                 f"  ({sum(m['status'] == 'split' for m in meta['slides'])} split)"
                 + (f"   {len(flagged)} need a human" if flagged else "") + f"   {time.time() - t0:.0f}s")
        meta["flagged"] = len(flagged)
        (out / "meta.json").write_text(json.dumps(meta, indent=2))
        tags = " ".join("#" + t.lstrip("#") for t in s.get("hashtags", []))
        (out / "caption.txt").write_text(f"{s.get('caption', '')}\n\n{tags}\n".lstrip("\n"))
        (out / "report.txt").write_text(f"{s['id']}\n" + "\n".join(self.log) + "\n")
        sheet(out, quiet=True)
        out.rename(final)
        self.say(f"  preview -> {final / '_sheet.jpg'}")
        if consume:
            used = Path(a.covers) / "used"
            used.mkdir(exist_ok=True)
            shutil.move(str(cover_path), str(used / cover_path.name))
            self.say(f"  cover consumed -> {used / cover_path.name}")
        return final


# ---------------------------------------------------------------- sheet

def sheet(out_dir, cols=None, quiet=False):
    """every slide in one image, tenths grid, so a set is one look instead of six."""
    pngs = sorted(Path(out_dir).glob("[0-9][0-9].png"))
    if not pngs:
        sys.exit(f"no slides in {out_dir}")
    first = Image.open(pngs[0])
    portrait = first.height > first.width
    tw = 420 if portrait else 760
    cols = cols or (3 if portrait else 2)
    th = round(first.height * tw / first.width)
    sh = Image.new("RGB", (cols * (tw + 8) + 8, ((len(pngs) + cols - 1) // cols) * (th + 36) + 8), (18, 18, 18))
    d = ImageDraw.Draw(sh)
    for i, p in enumerate(pngs):
        t = Image.open(p).convert("RGB").resize((tw, th), Image.LANCZOS)
        g = ImageDraw.Draw(t)
        for k in range(1, 10):
            c = (255, 90, 90) if k == 5 else (120, 120, 120)
            g.line([(tw * k // 10, 0), (tw * k // 10, th)], fill=c)
            g.line([(0, th * k // 10), (tw, th * k // 10)], fill=c)
        x, y = 8 + (i % cols) * (tw + 8), 8 + (i // cols) * (th + 36)
        sh.paste(t, (x, y))
        d.text((x + 4, y + th + 8), p.stem, fill=(230, 230, 230))
    dest = Path(out_dir) / "_sheet.jpg"
    sh.save(dest, quality=88)
    if not quiet:
        print(f"  sheet -> {dest}")
    return dest


# ---------------------------------------------------------------- check / probe

def load_script(path):
    s = json.loads(Path(path).read_text())
    base = Path(path).resolve().parent
    s.setdefault("id", Path(path).stem)
    if s.get("cover_photo"):                 # photo paths are relative to the script file
        s["cover_photo"] = str((base / s["cover_photo"]).resolve())
    for sl in s.get("slides", []):
        if sl.get("photo"):
            sl["photo"] = str((base / sl["photo"]).resolve())
    return s


def check(s, a):
    """hard errors exit 1; warnings never fail."""
    err, warn = [], []
    if not isinstance(s.get("cover"), list) or not s["cover"] or not str(s["cover"][0]).strip():
        err.append("cover must be a list: [hook] or [hook, subtitle]")
    if not s.get("slides"):
        err.append("slides is empty")
    for i, sl in enumerate(s.get("slides", []), 2):
        if not str(sl.get("head", "")).strip() and not str(sl.get("body", "")).strip():
            err.append(f"slide {i:02d} has no text")
        if sl.get("photo") and not Path(sl["photo"]).is_file():
            err.append(f"slide {i:02d} photo missing: {sl['photo']}")
        n = len(str(sl.get("body", "")))
        if n > a.body_max:
            warn.append(f"slide {i:02d} body is {n} chars (> {a.body_max}): long bodies find a legal "
                        f"spot far less often")
        if not FONT["emoji"] and any(is_emoji(c) for c in str(sl.get("head", "")) + str(sl.get("body", ""))):
            warn.append(f"slide {i:02d} has emoji but no --emoji-font: they may draw as boxes")
    if s.get("cover_photo") and not Path(s["cover_photo"]).is_file():
        err.append(f"cover_photo missing: {s['cover_photo']}")
    if s.get("inks") and not all(n in INKS for n in s["inks"]):
        err.append(f"inks must be two of {list(INKS)}")
    if getattr(a, "out", None) and (Path(a.out) / s["id"]).exists():
        err.append(f"output id exists: {Path(a.out) / s['id']} — pick a new id (never overwritten)")
    if getattr(a, "covers", None) and not s.get("cover_photo") and not list_images(a.covers):
        err.append(f"no cover photos in {a.covers}")
    if getattr(a, "backgrounds", None):
        need = sum(1 for sl in s.get("slides", []) if not sl.get("photo"))
        have = len(list_images(a.backgrounds))
        if have < need:
            err.append(f"{a.backgrounds} has {have} photos, needs {need}")
        elif have < need * a.audition:
            warn.append(f"{have} backgrounds for {need} slides x {a.audition} candidates: "
                        f"each slide sees ~{have // max(1, need)} photos, expect more flags")
    for m in err:
        print(f"  x {m}")
    for m in warn:
        print(f"  ! {m}")
    if not err and not warn:
        print("  clean")
    return not err


def probe(s, a):
    """no render, nothing consumed: on a sample of the pool, how often each slide finds a
    legal spot. flagged odds per slide = (1 - share) ** audition."""
    pool = list_images(a.backgrounds)
    if not pool:
        sys.exit(f"no photos in {a.backgrounds}")
    rng = random.Random(a.seed or 0)
    sample = rng.sample(pool, min(a.sample, len(pool)))
    inks = tuple(a.inks.split("/")) if a.inks else tuple(s.get("inks") or ("white", "yellow"))
    floor = GRADE_MIN if max(INKS[n][1] for n in inks) >= 128 else 0.0
    band = (int(H * SAFE_TOP), int(H * 0.83))
    frames = [(p, plan_crop(load(p), f"bg:{p.name}", band, 0)[0]) for p in sample]
    print(f"  {len(sample)} photos, inks {inks[0]}/{inks[1]}, audition {a.audition}\n")
    print("  slide  chars  legal  split  flag-odds")
    for i, sl in enumerate(s["slides"], 2):
        ok = split = 0
        for p, fr in frames:
            plan = plan_slide(fr, sl.get("head", ""), sl.get("body", ""), f"bg:{p.name}", inks, floor,
                              f"probe|{i}|{p.name}", fallback=False)
            ok += plan is not None
            split += bool(plan) and plan["status"] == "split"
        share = ok / len(frames)
        odds = (1 - share) ** a.audition
        print(f"  {i:02d}     {len(str(sl.get('body', ''))):4}   {share:4.0%}   {split:4}   {odds:6.1%}"
              + ("   <- cut the body" if odds > 0.05 else ""))


# ---------------------------------------------------------------- cli

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p, need_font=True):
        p.add_argument("script", help="carousel JSON (see SKILL.md)")
        p.add_argument("--frame", choices=list(FRAMES), default="portrait")
        p.add_argument("--font", required=need_font, help="path to a bold .ttf/.otf (none shipped)")
        p.add_argument("--weight", type=int, default=700, help="weight axis for a variable font")
        p.add_argument("--emoji-font", help="colour emoji font, e.g. Apple Color Emoji / NotoColorEmoji")
        p.add_argument("--inks", help="pin head/body inks, e.g. white/yellow (default: audition)")
        p.add_argument("--audition", type=int, default=SLIDE_AUDITION, help="photos tried per slide")
        p.add_argument("--seed", type=int, help="reproduce a render (default: random, saved in meta.json)")
        p.add_argument("--backgrounds", required=True, help="slide photo pool (reused, never twice per post)")

    p = sub.add_parser("render", help="render one carousel")
    common(p)
    p.add_argument("--covers", required=True, help="cover photo pool (CONSUMED: winner moves to used/)")
    p.add_argument("--out", required=True, help="output root; writes <out>/<id>/")
    p.add_argument("--body-max", type=int, default=80, help=argparse.SUPPRESS)

    p = sub.add_parser("check", help="validate a script + pools before rendering (exit 1 on error)")
    common(p, need_font=False)
    p.add_argument("--covers")
    p.add_argument("--out")
    p.add_argument("--body-max", type=int, default=80, help="warn above this many body characters")

    p = sub.add_parser("probe", help="placement odds per slide, no render, nothing consumed")
    common(p)
    p.add_argument("--sample", type=int, default=20, help="pool photos to test against")

    p = sub.add_parser("sheet", help="rebuild the preview sheet of an output folder")
    p.add_argument("out_dir")

    a = ap.parse_args()
    if a.cmd == "sheet":
        sheet(a.out_dir)
        return
    set_frame(a.frame)
    FONT.update(path=a.font, weight=a.weight, emoji=a.emoji_font)
    if a.inks and not all(n in INKS for n in a.inks.split("/")):
        sys.exit(f"--inks must be two of {list(INKS)}, e.g. white/yellow")
    if not sal.HAVE_VISION:
        print("  ! Apple Vision unavailable: no face / person / printed-text / object detection. "
              "gates run on edges and skin colour only — check every slide by eye")
    s = load_script(a.script)
    if a.cmd == "check":
        sys.exit(0 if check(s, a) else 1)
    if a.cmd == "probe":
        probe(s, a)
        return
    if not check(s, a):
        sys.exit(1)
    print(f"{s['id']}  ({a.frame} {W}x{H})")
    out = Carousel(s, a).render()
    print(f"done -> {out}")


if __name__ == "__main__":
    main()
