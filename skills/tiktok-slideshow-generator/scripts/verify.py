#!/usr/bin/env python3
"""Prove every rendered slide is a unique file and carries no metadata.

    verify.py OUTPUT_ROOT [MORE ...]      # e.g. verify.py out/

Checks every *.png / *.jpg under the folders (files starting with "_" or "." are skipped —
_sheet.jpg is review material and is never posted):

  1. EXACT DUPLICATES  identical SHA-256 between any two files. must be zero. FAIL.
  2. METADATA          any EXIF, text chunk, ICC profile or other info Pillow can see —
                       a camera model, a software tag or a source id ties a file to its origin
                       or to a tool. must be zero. FAIL.
  3. NEAR DUPLICATES   64-bit average hash, compared only ACROSS posts (slides inside one post
                       are a designed set and look alike). distance <= 2 is flagged: a reused
                       background that was not re-cropped. a review signal, never a FAIL.

Exit 1 if 1 or 2 fail.
"""
import argparse
import hashlib
import sys
from itertools import combinations
from pathlib import Path

from PIL import Image

NEAR = 2


def ahash(p):
    g = Image.open(p).convert("L").resize((8, 8), Image.LANCZOS)
    px = list(g.tobytes())
    avg = sum(px) / len(px)
    return sum(1 << i for i, v in enumerate(px) if v >= avg)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("roots", nargs="+", help="output folder(s) to scan")
    a = ap.parse_args()
    files = sorted(p for r in a.roots for p in Path(r).rglob("*")
                   if p.suffix.lower() in (".png", ".jpg", ".jpeg") and p.is_file()
                   and not any(part.startswith(("_", ".")) for part in p.relative_to(r).parts))
    if not files:
        sys.exit("no images found")
    print(f"scanning {len(files)} image(s)")
    fails = 0

    by_hash = {}
    for p in files:
        by_hash.setdefault(hashlib.sha256(p.read_bytes()).hexdigest(), []).append(p)
    dups = [g for g in by_hash.values() if len(g) > 1]
    if dups:
        fails += 1
        print("FAIL  exact duplicates:")
        for g in dups:
            print("   " + "  ==  ".join(map(str, g)))
    else:
        print("ok    every file is byte-unique")

    meta = []
    for p in files:
        with Image.open(p) as im:
            keys = sorted(set(im.info) | ({"exif"} if im.getexif() else set()))
        if keys:
            meta.append((p, keys))
    if meta:
        fails += 1
        print("FAIL  metadata found:")
        for p, k in meta:
            print(f"   {p}: {', '.join(k)}")
    else:
        print("ok    metadata-bare (no exif, text chunks or icc)")

    hs = [(p, ahash(p)) for p in files]
    near = sorted((bin(ha ^ hb).count("1"), pa, pb) for (pa, ha), (pb, hb) in combinations(hs, 2)
                  if pa.parent != pb.parent)
    close = [t for t in near if t[0] <= NEAR]
    if close:
        print(f"note  {len(close)} near-identical pair(s) across posts (reused photo, same crop?):")
        for d, pa, pb in close[:10]:
            print(f"   dist {d}  {pa}  ~  {pb}")
    elif near:
        print(f"ok    no near-duplicates across posts (closest distance {near[0][0]} of 64)")

    if fails:
        sys.exit(f"{fails} check(s) FAILED")
    print("PASS")


if __name__ == "__main__":
    main()
