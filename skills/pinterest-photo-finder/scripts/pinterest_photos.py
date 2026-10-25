#!/usr/bin/env python3
"""Pinterest photo finder: free search, original-size download, quality gates, duplicate removal.

    python3 pinterest_photos.py OUT --q "moody bedroom lamp" --q "girl reading aesthetic" --pages 3
    python3 pinterest_photos.py OUT --q "..." --lum 0 110 --min-side 1080 --sheet   # dark pool + sheets
    python3 pinterest_photos.py OUT --sheet                                        # sheets only

Every kept photo is an upright RGB JPEG named by the hash of the downloaded bytes.
Rejects are counted by reason and never kept. OUT/_sources.jsonl says which query found
which file. Sheets go to OUT/_sheets/ with a number on every tile, so a human can pick.

Duplicates: Apple Vision feature prints on macOS (catches crops and re-encodes), else a
pure-Python 64-bit perceptual hash (DCT), which is weaker on crops. --dedup picks one.
"""
import argparse, hashlib, http.cookiejar, io, json, subprocess, sys, tempfile, time, urllib.parse, urllib.request
from collections import Counter
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageOps

try:
    import pillow_heif
    pillow_heif.register_heif_opener()
except ImportError:
    pass

BROWSER = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
API_HEADERS = {"User-Agent": BROWSER, "X-Requested-With": "XMLHttpRequest",
               "Accept": "application/json, text/javascript, */*; q=0.01",
               "X-Pinterest-PWS-Handler": "www/search/[scope].js"}
# The resource API answers 403 "Invalid Resource Request" without the cookies the search
# page sets, without the PWS-Handler header, or with source_url not fully percent-encoded.
OPENER = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
PHOTO_EXT = {".jpg", ".jpeg", ".png", ".webp", ".heic"}


def search(q, pages):
    """Yield (original_url, width, height) for every pin on `pages` result pages (~50 each)."""
    page = "/search/pins/?q=" + urllib.parse.quote(q) + "&rs=typed"
    try:  # sets the session cookies the API needs
        OPENER.open(urllib.request.Request("https://www.pinterest.com" + page,
                                           headers={"User-Agent": BROWSER}), timeout=30).read()
    except Exception as e:
        print("  search page failed:", q, e, file=sys.stderr)
    bookmark = None
    for _ in range(pages):
        opts = {"query": q, "scope": "pins", "page_size": 50}
        if bookmark:
            opts["bookmarks"] = [bookmark]
        url = ("https://www.pinterest.com/resource/BaseSearchResource/get/?source_url="
               + urllib.parse.quote(page, safe="")
               + "&data=" + urllib.parse.quote(json.dumps({"options": opts, "context": {}}, separators=(",", ":"))))
        try:
            d = json.loads(OPENER.open(urllib.request.Request(url, headers=API_HEADERS), timeout=30).read())
        except Exception as e:
            print("  search failed:", q, e, file=sys.stderr)
            return
        rr = d.get("resource_response", {})
        for pin in (rr.get("data") or {}).get("results") or []:
            o = (pin.get("images") or {}).get("orig")  # i.pinimg.com/originals = biggest file stored
            if o and o.get("url"):
                yield o["url"], o.get("width") or 0, o.get("height") or 0
        bookmark = rr.get("bookmark") or ((d.get("resource") or {}).get("options", {}).get("bookmarks") or [None])[0]
        if not bookmark or bookmark == "-end-":
            return
        time.sleep(1.0)


def load(raw):
    """Bytes -> upright RGB. Some '.jpg' pins are HEIC: pillow-heif, else macOS sips."""
    try:
        img = Image.open(io.BytesIO(raw))
        img.load()
    except Exception:
        with tempfile.TemporaryDirectory() as t:
            src, dst = Path(t) / "x.heic", Path(t) / "x.jpg"
            src.write_bytes(raw)
            subprocess.run(["sips", "-s", "format", "jpeg", str(src), "--out", str(dst)], capture_output=True)
            img = Image.open(dst)
            img.load()
    return ImageOps.exif_transpose(img).convert("RGB")  # bake rotation into pixels; saved file has no tag


def sharpness(img):
    """99th percentile of |Laplacian| at 720px wide. Under ~45 looked blurry at zoom."""
    g = np.asarray(ImageOps.grayscale(img).resize((720, max(1, round(720 * img.height / img.width)))), float)
    lap = np.abs(4 * g[1:-1, 1:-1] - g[:-2, 1:-1] - g[2:, 1:-1] - g[1:-1, :-2] - g[1:-1, 2:])
    return float(np.percentile(lap, 99))


def luminance(img):
    return float(np.asarray(ImageOps.grayscale(img).resize((96, 72)), float).mean())


# --- duplicate fingerprints -------------------------------------------------------------

def vision_print(img):
    """Apple Vision feature print (macOS + pyobjc-framework-Vision). L2 distance <= 0.37 = same photo."""
    import Vision
    from Foundation import NSData
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=90)
    req = Vision.VNGenerateImageFeaturePrintRequest.alloc().init()
    data = NSData.dataWithBytes_length_(buf.getvalue(), len(buf.getvalue()))
    Vision.VNImageRequestHandler.alloc().initWithData_options_(data, None).performRequests_error_([req], None)
    obs = (req.results() or [None])[0]
    if obs is None:
        return None
    # computeDistance's out-param fails under pyobjc, so compare the raw vectors ourselves
    return np.frombuffer(bytes(obs.data()), dtype=np.float32 if obs.elementType() == 1 else np.float64).astype(np.float32)


_N = 32
_DCT = np.cos(np.pi * (2 * np.arange(_N)[None, :] + 1) * np.arange(_N)[:, None] / (2 * _N))


def phash(img):
    """64-bit DCT perceptual hash, pure numpy. Hamming distance <= 10 of 64 = same photo."""
    g = np.asarray(ImageOps.grayscale(img).resize((_N, _N), Image.LANCZOS), float)
    low = (_DCT @ g @ _DCT.T)[:8, :8].flatten()
    return low > np.median(low[1:])


class Dedup:
    def __init__(self, mode, same):
        if mode == "auto":
            try:
                import Vision  # noqa: F401
                mode = "vision"
            except ImportError:
                mode = "phash"
        self.mode, self.prints = mode, []
        self.fp = vision_print if mode == "vision" else phash
        self.same = same if same is not None else (0.37 if mode == "vision" else 10)

    def dist(self, a, b):
        return float(np.linalg.norm(a - b)) if self.mode == "vision" else int(np.count_nonzero(a != b))

    def is_new(self, img):
        """True and remembers the photo when nothing known is within the `same` distance."""
        fp = self.fp(img)
        if fp is None or any(self.dist(fp, p) <= self.same for p in self.prints):
            return False
        self.prints.append(fp)
        return True


# --- commands -----------------------------------------------------------------------------

def hunt(a):
    out = Path(a.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    dd = Dedup(a.dedup, a.same)
    for d in [out, *map(Path, a.against)]:
        for f in sorted(d.glob("*")):
            if f.suffix.lower() in PHOTO_EXT:
                try:
                    dd.is_new(load(f.read_bytes()))
                except Exception:
                    pass
    print(f"dedup: {dd.mode} (same <= {dd.same}); {len(dd.prints)} photos already known (out + --against)")
    seen, why = set(), Counter()
    for q in a.q:
        kept_q = 0
        for url, w, h in search(q, a.pages):
            if url in seen:
                continue
            seen.add(url)
            if w and h and (min(w, h) < a.min_side or w / h < a.min_ratio):
                why["size/ratio (listed)"] += 1  # skipped before download
                continue
            try:
                raw = urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": BROWSER}), timeout=30).read()
                img = load(raw)
            except Exception:
                why["download"] += 1
                continue
            name = hashlib.md5(raw).hexdigest() + ".jpg"
            if (out / name).exists():
                why["repeat"] += 1
                continue
            if min(img.size) < a.min_side or img.width / img.height < a.min_ratio:
                why["size/ratio"] += 1
                continue
            if not a.lum[0] <= luminance(img) <= a.lum[1]:
                why["brightness"] += 1
                continue
            if sharpness(img) < a.sharp:
                why["blurry"] += 1
                continue
            if not dd.is_new(img):
                why["repeat"] += 1
                continue
            img.save(out / name, quality=93)
            with open(out / "_sources.jsonl", "a") as log:
                log.write(json.dumps({"file": name, "q": q, "url": url}) + "\n")
            kept_q += 1
            why["KEPT"] += 1
        print(f"  {kept_q:3} kept  <- {q}", flush=True)
    print(dict(why.most_common()))


def sheet(a):
    out = Path(a.out_dir)
    files = sorted(f for f in out.glob("*") if f.suffix.lower() in PHOTO_EXT)
    if not files:
        print("no photos in", out)
        return
    (out / "_sheets").mkdir(exist_ok=True)
    tw, cols, per = a.tile, 6, 24
    font = ImageFont.load_default(size=max(16, tw // 16))
    with open(out / "_sheets" / "index.txt", "w") as idx:
        for s in range(0, len(files), per):
            batch = files[s:s + per]
            sh = Image.new("RGB", (tw * cols, (tw + tw // 10) * ((len(batch) + cols - 1) // cols)), "white")
            for k, (n, f) in enumerate(zip(range(s + 1, s + 1 + len(batch)), batch)):
                im = ImageOps.exif_transpose(Image.open(f)).convert("RGB")
                im.thumbnail((tw - 8, tw - 8))
                x, y = (k % cols) * tw, (k // cols) * (tw + tw // 10)
                sh.paste(im, (x + (tw - im.width) // 2, y + 4))
                ImageDraw.Draw(sh).text((x + 8, y + tw), f"{n}  {f.stem[:8]}", fill="black", font=font)
                idx.write(f"{n}\t{f.name}\n")
            p = out / "_sheets" / f"{s // per + 1}.jpg"
            sh.save(p, quality=82)
            print(p)
    print(out / "_sheets" / "index.txt", "(number -> file)")


def main():
    ap = argparse.ArgumentParser(description="Search Pinterest for free, download full-size originals, drop "
                                             "blurry / small / off-brightness / duplicate photos, make numbered sheets.")
    ap.add_argument("out_dir", help="folder for kept photos (also read for duplicates)")
    ap.add_argument("--q", action="append", default=[], help="search words; repeat for more queries")
    ap.add_argument("--pages", type=int, default=3, help="result pages per query, ~50 pins each (default 3)")
    ap.add_argument("--lum", type=float, nargs=2, default=(0, 255), metavar=("MIN", "MAX"),
                    help="keep mean brightness in this 0-255 band, e.g. 0 110 for a dark pool (default all)")
    ap.add_argument("--min-side", type=int, default=900, help="shortest side in px (default 900)")
    ap.add_argument("--min-ratio", type=float, default=0.0,
                    help="min width/height; 0.74 drops tall pins, 1.3 keeps landscape only (default off)")
    ap.add_argument("--sharp", type=float, default=45, help="min sharpness, p99 |Laplacian| at 720px (default 45)")
    ap.add_argument("--against", action="append", default=[],
                    help="another folder whose photos count as already had (e.g. your pool or a cut list)")
    ap.add_argument("--dedup", choices=["auto", "vision", "phash"], default="auto",
                    help="auto = Apple Vision on macOS, else pure-Python phash")
    ap.add_argument("--same", type=float, default=None,
                    help="duplicate distance cut (default 0.37 vision, 10 phash)")
    ap.add_argument("--sheet", action="store_true", help="write numbered contact sheets to OUT/_sheets/")
    ap.add_argument("--tile", type=int, default=480, help="sheet tile width in px (default 480)")
    a = ap.parse_args()
    if not a.q and not a.sheet:
        ap.error("give --q (search) and/or --sheet")
    if a.q:
        hunt(a)
    if a.sheet:
        sheet(a)


if __name__ == "__main__":
    main()
