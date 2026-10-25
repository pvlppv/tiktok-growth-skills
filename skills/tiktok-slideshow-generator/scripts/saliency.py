#!/usr/bin/env python3
"""Where the eye goes in a photo, and where type may never sit.

Two kinds of answer, kept apart on purpose:

  COSTS  (profile, cost_map, clear_map, ink_cost_map) rank legal spots against each other.
  GATES  (gate, gate_map, surface_map) refuse a spot outright. Costs always produce a winner,
         so a photo that is bad everywhere still gets text on its least-bad spot; a gate is
         what stops a paragraph landing across bare legs or a printed page.

On macOS it asks Apple Vision (on-device, free, no key) for faces, attention, objectness,
people, printed text and labelled objects. Anywhere else, or with SLIDES_NO_VISION=1, it
runs on gradient energy and a skin-colour test only, and says so.

Every threshold here is set for 30 px square cells. `configure(w, h)` re-derives the
grid from the frame so the cell stays 30 px whatever the frame is; call it before anything.

Debug one photo:  python3 saliency.py photo.jpg [--out mask.png]
"""
import argparse
import io
import os

import numpy as np
from PIL import Image, ImageFilter
from scipy import ndimage

try:
    import pillow_heif                       # some downloads are HEIC with a .jpg name
    pillow_heif.register_heif_opener()
except Exception:
    pass

HAVE_VISION = False
if not os.environ.get("SLIDES_NO_VISION"):
    try:
        import Vision
        import Quartz
        from Foundation import NSData
        HAVE_VISION = True
    except Exception:
        pass

GW, GH = 64, 36          # cell grid; reset by configure()
W_REF = 1920.0
ANALYSIS_W = 512         # Vision is fed a downscale; its saliency maps are ~68 px anyway

# --- cost weights. a face OUTVOTES everything: one row of face costs more than the busiest
# photo ever accepted. width must not discount a face — text across a narrow face is as broken.
FACE_WEIGHT = 10.0
ATTENTION_WEIGHT = 1.0
OBJECT_WEIGHT = 0.55
FACE_PAD_Y, FACE_PAD_X = 0.55, 0.25   # Vision's face box has no hair or chin; grow it
PERSON_WEIGHT = 1.1      # clothing: mild. type on a plain top can read deliberate
SKIN_WEIGHT = 6.0        # skin: effectively off limits
SKIN_TOLERANCE = 14.0    # chroma distance from this person's own face that still counts as skin
COST_SCALE = 22.0
DETAIL_REF = 26.0        # gradient energy scale: open wall ~0, printed page ~1
DETAIL_WEIGHT = 1.6
STRADDLE_WEIGHT = 2.8    # spanning two materials costs more than sitting on one busy one
KEEP_BONUS = 26.0        # an intact object in frame is worth something
CUT_FACE_COST = 900.0    # a face sliced by the crop window: no placement is worth half a head
KEEP_FACE_BONUS = 340.0  # a whole face kept — else "crop the head off" is the cheapest fix
CUT_OBJECT_COST = 260.0

# --- gates. too wide a pad forbids so much that the search falls through to its ungated
# fallback more often than it places — stricter gates make output WORSE.
GATE_EDGE = 45.0         # per-pixel gradient that means an object line, not a fabric fold
GATE_EDGE_COVER = 95.0   # cover only: folds and seams pass, a doorframe or laptop lid does not
GATE_PAD = 0.012         # clearance grown around everything forbidden, share of frame width
GATE_PAD_COVER = 0.006
GATE_CELL = 0.35         # share of a cell that must be forbidden before the cell blocks a window
GATE_PIX = 512
GATE_OBJ_CONF = 0.55
GATE_OBJ_MAX = 0.55      # a labelled box bigger than this says "the subject is in the photo"
SRC_CELLS = 160
INK_SPREAD_K = 1.3       # ink must survive mean +/- 1.3 sigma, not just row means (mottled rock)

_cache = {}


def configure(w, h):
    """keep cells at 30 px for any frame — every threshold above is set for that size."""
    global GW, GH, W_REF
    GW, GH, W_REF = max(8, round(w / 30.0)), max(8, round(h / 30.0)), float(w)


def _cached(tag, key, fn):
    """memoise per frame. key None = never cache (a one-off image)."""
    if key is None:
        return fn()
    key = (tag, key)
    if key not in _cache:
        _cache[key] = fn()
    return _cache[key]


# ---------------------------------------------------------------- Vision, one pass per image

def _vision(img):
    """every Vision cue for one image, top-left normalised boxes. empty lists without Vision."""
    out = {"faces": [], "salient": [], "text": [], "objects": [], "person": None}
    if not HAVE_VISION:
        return out
    small = img.convert("RGB")
    if small.width > ANALYSIS_W:
        small = small.resize((ANALYSIS_W, max(1, round(small.height * ANALYSIS_W / small.width))))
    buf = io.BytesIO()
    small.save(buf, "JPEG", quality=80)
    data = NSData.dataWithBytes_length_(buf.getvalue(), len(buf.getvalue()))
    handler = Vision.VNImageRequestHandler.alloc().initWithData_options_(data, {})

    def make(name, setup=None):
        try:
            r = getattr(Vision, name).alloc().init()
            if setup:
                setup(r)
            return r
        except Exception:
            return None

    def fast_text(r):
        r.setRecognitionLevel_(1)            # fast: only WHERE the words are matters
        r.setUsesLanguageCorrection_(False)

    reqs = {"face": make("VNDetectFaceRectanglesRequest"),
            "att": make("VNGenerateAttentionBasedSaliencyImageRequest"),
            "obj": make("VNGenerateObjectnessBasedSaliencyImageRequest"),
            "text": make("VNRecognizeTextRequest", fast_text),
            "label": make("VNRecognizeObjectsRequest"),
            "person": make("VNGeneratePersonSegmentationRequest", lambda r: r.setQualityLevel_(1))}
    reqs = {k: v for k, v in reqs.items() if v is not None}
    try:
        handler.performRequests_error_(list(reqs.values()), None)
    except Exception:
        return out

    def res(k):
        try:
            return list(reqs[k].results() or []) if k in reqs else []
        except Exception:
            return []

    def box(b):                                  # Vision is bottom-left; flip once, here
        return b.origin.x, 1.0 - b.origin.y - b.size.height, b.size.width, b.size.height

    out["faces"] = [box(o.boundingBox()) for o in res("face")]
    for k, wt in (("att", ATTENTION_WEIGHT), ("obj", OBJECT_WEIGHT)):
        for o in res(k):
            for s in list(o.salientObjects() or []):
                out["salient"].append((*box(s.boundingBox()), wt, k))
    out["text"] = [box(o.boundingBox()) for o in res("text")]
    for o in res("label"):
        try:
            labels = list(o.labels() or [])
            if labels and labels[0].confidence() >= GATE_OBJ_CONF:
                x, y, w, h = box(o.boundingBox())
                if w * h <= GATE_OBJ_MAX:
                    out["objects"].append((x, y, w, h))
        except Exception:
            continue
    seg = res("person")
    if seg:
        try:
            pb = seg[0].pixelBuffer()
            w, h = Quartz.CVPixelBufferGetWidth(pb), Quartz.CVPixelBufferGetHeight(pb)
            Quartz.CVPixelBufferLockBaseAddress(pb, 1)
            try:
                stride = Quartz.CVPixelBufferGetBytesPerRow(pb)
                raw = Quartz.CVPixelBufferGetBaseAddress(pb).as_buffer(stride * h)
                out["person"] = np.frombuffer(raw, dtype=np.uint8).reshape(h, stride)[:, :w].copy()
            finally:
                Quartz.CVPixelBufferUnlockBaseAddress(pb, 1)
        except Exception:
            pass
    return out


def vision(img, key=None):
    return _cached("vision", key, lambda: _vision(img))


def _cost_boxes(v):
    """(x, y, w, h, weight): padded faces at full weight, saliency boxes damped by area —
    objectness returns near-full-frame boxes on selfies, which carry no information."""
    out = [(x - w * FACE_PAD_X, y - h * FACE_PAD_Y, w * (1 + 2 * FACE_PAD_X),
            h * (1 + 2 * FACE_PAD_Y), FACE_WEIGHT) for x, y, w, h in v["faces"]]
    for x, y, w, h, wt, _k in v["salient"]:
        wt = wt * max(0.0, 1.0 - w * h) ** 2
        if wt > 0.01:
            out.append((x, y, w, h, wt))
    return out


def _person_cells(v, cells):
    gh, gw = cells
    if v["person"] is None:
        return np.zeros((gh, gw), dtype=np.float32)
    m = Image.fromarray(v["person"]).resize((gw, gh), Image.BILINEAR)
    return np.asarray(m, dtype=np.float32) / 255.0


def _skin_like_face(img, faces, cells):
    """skin by chroma sampled from THIS person's face, so it works across skin tones."""
    gh, gw = cells
    if not faces:
        return np.zeros((gh, gw), dtype=np.float32)
    a = np.asarray(img.convert("YCbCr").resize((gw, gh), Image.BILINEAR), dtype=np.float32)
    cb, cr = a[..., 1], a[..., 2]
    ref = []
    for fx, fy, fw, fh in faces:
        c0, c1 = int((fx + fw * .25) * gw), int((fx + fw * .75) * gw)
        r0, r1 = int((fy + fh * .25) * gh), int((fy + fh * .75) * gh)
        if c1 > c0 and r1 > r0:
            ref.append((cb[r0:r1, c0:c1].mean(), cr[r0:r1, c0:c1].mean()))
    if not ref:
        return np.zeros((gh, gw), dtype=np.float32)
    mcb, mcr = np.mean([r[0] for r in ref]), np.mean([r[1] for r in ref])
    dist = np.sqrt((cb - mcb) ** 2 + (cr - mcr) ** 2)
    return np.clip(1.0 - dist / SKIN_TOLERANCE, 0.0, 1.0).astype(np.float32)


def _detail(img, gw, gh, sub=4):
    g = np.asarray(img.convert("L").resize((gw * sub, gh * sub), Image.BILINEAR), dtype=np.float32)
    e = np.abs(np.diff(g, axis=1, append=g[:, -1:])) + np.abs(np.diff(g, axis=0, append=g[-1:, :]))
    cells = e.reshape(gh, sub, gw, sub).mean(axis=(1, 3))
    return np.clip(cells / DETAIL_REF, 0.0, 1.6) * DETAIL_WEIGHT


def _stamp(grid, x, y, w, h, weight):
    gh, gw = grid.shape
    c0, c1 = int(np.clip(x, 0, 1) * gw), int(np.ceil(np.clip(x + w, 0, 1) * gw))
    r0, r1 = int(np.clip(y, 0, 1) * gh), int(np.ceil(np.clip(y + h, 0, 1) * gh))
    if c1 > c0 and r1 > r0:
        grid[r0:r1, c0:c1] += weight


def profile(img, key=None):
    """GH x GW "don't put text here" cost: detail + person/skin + face and saliency boxes."""
    def build():
        v = vision(img, key)
        grid = _detail(img, GW, GH)
        person = _person_cells(v, (GH, GW))
        grid += person * PERSON_WEIGHT + _skin_like_face(img, v["faces"], (GH, GW)) * person * SKIN_WEIGHT
        for b in _cost_boxes(v):
            _stamp(grid, *b)
        return grid
    return _cached("profile", key, build)


# ---------------------------------------------------------------- crop planning in SOURCE space
# a head half outside the window returns no face from Vision, so the framing that sliced it
# scores clean. so the SOURCE is analysed once and candidate windows are scored as arithmetic.

def analyze_source(img, key=None):
    def build():
        w, h = img.size
        gw = SRC_CELLS if w >= h else max(8, round(SRC_CELLS * w / h))
        gh = SRC_CELLS if h > w else max(8, round(SRC_CELLS * h / w))
        v = vision(img, None if key is None else ("src", key))
        grid = _detail(img, gw, gh, sub=2)
        person = _person_cells(v, (gh, gw))
        grid += person * PERSON_WEIGHT + _skin_like_face(img, v["faces"], (gh, gw)) * person * SKIN_WEIGHT
        objects = [(x, y, bw, bh, wt) for x, y, bw, bh, wt, _k in v["salient"]]
        return {"grid": grid, "faces": v["faces"], "objects": objects}
    return _cached("src", key, build)


def window_grid(src, rect):
    """cost map of one candidate window in the output's cell layout, faces and objects
    STAMPED IN — without the stamp the cover was placed on a map with no faces in it."""
    g = src["grid"]
    H_, W_ = g.shape
    x0, y0, x1, y1 = rect
    c0, c1 = int(x0 * W_), max(int(x0 * W_) + 1, int(round(x1 * W_)))
    r0, r1 = int(y0 * H_), max(int(y0 * H_) + 1, int(round(y1 * H_)))
    patch = g[r0:min(r1, H_), c0:min(c1, W_)]
    if patch.size == 0:
        return np.zeros((GH, GW), dtype=np.float32)
    ri = np.clip(np.arange(GH) * patch.shape[0] // GH, 0, patch.shape[0] - 1)
    ci = np.clip(np.arange(GW) * patch.shape[1] // GW, 0, patch.shape[1] - 1)
    out = patch[np.ix_(ri, ci)].astype(np.float32).copy()
    ww, wh = max(x1 - x0, 1e-9), max(y1 - y0, 1e-9)
    for fx, fy, fw, fh in src["faces"]:
        fx, fy = fx - fw * FACE_PAD_X, fy - fh * FACE_PAD_Y
        fw, fh = fw * (1 + 2 * FACE_PAD_X), fh * (1 + 2 * FACE_PAD_Y)
        _stamp(out, (fx - x0) / ww, (fy - y0) / wh, fw / ww, fh / wh, FACE_WEIGHT)
    for bx, by, bw, bh, wt in src["objects"]:
        wt = wt * max(0.0, 1.0 - bw * bh) ** 2
        _stamp(out, (bx - x0) / ww, (by - y0) / wh, bw / ww, bh / wh, wt)
    return out


def window_penalty(src, rect):
    """a face must be wholly in or wholly out; objects pay for what the window throws away."""
    x0, y0, x1, y1 = rect
    cost, kept = 0.0, 0.0
    for fx, fy, fw, fh in src["faces"]:
        ox, oy = min(x1, fx + fw) - max(x0, fx), min(y1, fy + fh) - max(y0, fy)
        if ox <= 0 or oy <= 0:
            cost += KEEP_FACE_BONUS              # cropping her out is never the cheap fix
            continue
        inside = ox * oy / max(fw * fh, 1e-9)
        cost += CUT_FACE_COST * (1 - inside) if inside < 0.985 else -KEEP_FACE_BONUS
    for bx, by, bw, bh, wt in src["objects"]:
        ox, oy = min(x1, bx + bw) - max(x0, bx), min(y1, by + bh) - max(y0, by)
        if ox > 0 and oy > 0:
            inside = ox * oy / max(bw * bh, 1e-9)
            cost += CUT_OBJECT_COST * wt * (1 - inside)
            kept += wt * inside * min(bw * bh * 4.0, 1.0)
    return cost - kept * KEEP_BONUS


def band_cost(grid, y0, y1, height):
    """full-width band: 92nd percentile + mean (one stray cell must not forbid a region,
    a mean lets a last line across a mouth average out) + spread (straddling two things)."""
    r0 = int(np.clip(y0 / height, 0, 1) * GH)
    r1 = max(r0 + 1, int(np.ceil(np.clip(y1 / height, 0, 1) * GH)))
    patch = grid[r0:min(r1, GH)]
    if patch.size == 0:
        return 0.0
    return float(np.percentile(patch, 92) + patch.mean() * 0.35 + patch.std() * STRADDLE_WEIGHT) * COST_SCALE


def subject_mass(grid):
    return float(grid.mean())


def lum_grid(img):
    return np.asarray(img.convert("L").resize((GW, GH), Image.BOX), dtype=np.float32)


def subject_side(img, key=None):
    """'left' / 'right' / None: which half the eye goes to, so the block can take the other."""
    def build():
        att = [s for s in vision(img, key)["salient"] if s[5] == "att"]
        if not att:
            return None
        x, _y, w, _h, *_ = max(att, key=lambda s: s[2] * s[3])
        c = x + w / 2
        return None if abs(c - 0.5) <= 0.08 else ("left" if c < 0.5 else "right")
    return _cached("side", key, build)


# ---------------------------------------------------------------- the hard gate

def _skin_pixels(img):
    """bare skin, face or no face — a cropped lap or arm has no face to sample from.
    absolute chroma range + a warm margin, so cream linen stays legal."""
    a = np.asarray(img.convert("RGB"), dtype=np.float32)
    r, g, b = a[..., 0], a[..., 1], a[..., 2]
    y = 0.299 * r + 0.587 * g + 0.114 * b
    cb = 128 - 0.168736 * r - 0.331264 * g + 0.5 * b
    cr = 128 + 0.5 * r - 0.418688 * g - 0.081312 * b
    return (cr > 141) & (cr < 176) & (cb > 76) & (cb < 124) & (y > 55) & (r > 80) & (r - b > 22)


def gate(img, key=None, relax=False):
    """GH x GW share of each cell type may never touch: skin, printed text, faces, hard
    object lines, labelled objects — all grown by a clearance margin.

    relax=True is the COVER: a hook photo is a person, so objects stop being forbidden and
    folds read as texture. face, skin and printed words stay refused."""
    def build():
        w, h = img.size
        small = img.convert("RGB").resize((GATE_PIX, max(1, round(GATE_PIX * h / w))), Image.BILINEAR)
        sw, sh = small.size
        bad = _skin_pixels(small)
        gy, gx = np.gradient(np.asarray(small.convert("L"), dtype=np.float32))
        bad |= np.hypot(gx, gy) > (GATE_EDGE_COVER if relax else GATE_EDGE)
        v = vision(img, key)
        boxes = v["text"] + [(x - fw * FACE_PAD_X, y - fh * FACE_PAD_Y, fw * (1 + 2 * FACE_PAD_X),
                              fh * (1 + 2 * FACE_PAD_Y)) for x, y, fw, fh in v["faces"]]
        if not relax:
            boxes += v["objects"]
        for bx, by, bw, bh in boxes:
            bad[max(0, int(by * sh)):min(sh, int((by + bh) * sh)),
                max(0, int(bx * sw)):min(sw, int((bx + bw) * sw))] = True
        pad = max(1, int((GATE_PAD_COVER if relax else GATE_PAD) * sw))
        if pad > 1:
            bad = np.asarray(Image.fromarray((bad * 255).astype(np.uint8))
                             .filter(ImageFilter.MaxFilter(pad * 2 + 1))) > 127
        ri = (np.arange(sh) * GH // sh).clip(0, GH - 1)
        ci = (np.arange(sw) * GW // sw).clip(0, GW - 1)
        out, cnt = np.zeros((GH, GW), np.float32), np.zeros((GH, GW), np.float32)
        np.add.at(out, (ri[:, None], ci[None, :]), bad.astype(np.float32))
        np.add.at(cnt, (ri[:, None], ci[None, :]), 1.0)
        return out / np.maximum(cnt, 1.0)
    return _cached(("gate", relax), key, build)


def surface_grid(img, key=None):
    """(step, texture) per cell. STEP = brightness jump to the neighbour cell — a boundary
    (sheet to jeans, wall to frame). TEXTURE = spread inside the cell — stripes, print.
    a smooth gradient across one surface scores near zero on both."""
    def build():
        g = np.asarray(img.convert("L").resize((GW * 4, GH * 4), Image.BILINEAR), dtype=np.float32)
        blocks = g.reshape(GH, 4, GW, 4).transpose(0, 2, 1, 3).reshape(GH, GW, 16)
        means, texture = blocks.mean(axis=-1), blocks.std(axis=-1)
        step = np.maximum(np.abs(np.diff(means, axis=1, append=means[:, -1:])),
                          np.abs(np.diff(means, axis=0, append=means[-1:, :])))
        return step, texture
    return _cached("surf", key, build)


def _windows(grid, cw, ch):
    gh, gw = grid.shape
    ch, cw = max(1, min(ch, gh)), max(1, min(cw, gw))
    win = np.lib.stride_tricks.sliding_window_view(grid, (ch, cw))
    return win.reshape(win.shape[0], win.shape[1], -1)


def surface_map(surf, cw, ch):
    return _windows(surf[0], cw, ch).mean(-1), _windows(surf[1], cw, ch).mean(-1)


def clear_grid(g):
    """cells to the nearest forbidden thing. a rectangle in the GAP between a book, a pen
    and a candle contains nothing forbidden; distance is what tells it from a pillow."""
    return ndimage.distance_transform_edt(g <= GATE_CELL).astype(np.float32)


def clear_map(c, cw, ch):
    return _windows(c, cw, ch).mean(-1)


def gate_map(g, cw, ch):
    """True wherever a cw x ch block would contain a forbidden cell."""
    return _windows(g, cw, ch).max(-1) > GATE_CELL


def ink_cost_map(lgrid, cw, ch, ink_lum, floor, weight):
    """penalty for this ink on a cw x ch block at every position. the block must survive its
    darkest and brightest ROW and its texture spread. clearing the floor costs nothing: this
    only pushes text off surfaces it can't hold, never toward the darkest object."""
    gh, gw = lgrid.shape
    ch, cw = max(1, min(ch, gh)), max(1, min(cw, gw))
    win = np.lib.stride_tricks.sliding_window_view(lgrid, (ch, cw))
    rows = win.mean(axis=-1)
    flat = win.reshape(win.shape[0], win.shape[1], -1)
    m, s = flat.mean(-1), flat.std(-1) * INK_SPREAD_K
    lo, hi = np.minimum(rows.min(-1), m - s), np.maximum(rows.max(-1), m + s)
    sep = np.minimum(np.abs(ink_lum - lo), np.abs(ink_lum - hi))
    return np.maximum(0.0, floor - sep) * weight


def cost_map(grid, cw, ch):
    """cost of a cw x ch block at every top-left cell: worst cell + mean + spread."""
    f = _windows(grid, cw, ch)
    return (f.max(-1) + f.mean(-1) * 0.35 + f.std(-1) * STRADDLE_WEIGHT) * COST_SCALE


def main():
    ap = argparse.ArgumentParser(description="Show what the engine sees in one photo: Vision "
                                 "cues and the forbidden-cell mask (red = type may never sit).")
    ap.add_argument("photo")
    ap.add_argument("--out", help="write the photo with the forbidden mask overlaid")
    ap.add_argument("--cover", action="store_true", help="use the relaxed cover gate")
    a = ap.parse_args()
    img = Image.open(a.photo).convert("RGB")
    configure(*img.size)
    v = vision(img)
    g = gate(img, relax=a.cover)
    print(f"vision: {'on' if HAVE_VISION else 'OFF (gradient + skin colour only)'}")
    print(f"faces {len(v['faces'])}  salient {len(v['salient'])}  text boxes {len(v['text'])}  "
          f"objects {len(v['objects'])}  person {'yes' if v['person'] is not None else 'no'}")
    print(f"forbidden cells: {(g > GATE_CELL).mean():.0%} of the frame")
    if a.out:
        red = Image.new("RGB", img.size, (255, 0, 0))
        m = Image.fromarray(((g > GATE_CELL) * 110).astype(np.uint8)).resize(img.size, Image.NEAREST)
        Image.composite(red, img, m).save(a.out)
        print(a.out)


if __name__ == "__main__":
    main()
