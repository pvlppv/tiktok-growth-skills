#!/usr/bin/env python3
"""teardown.py — read a rival's carousels slide by slide: what the slides say, how they are
built, and where and how the app is mentioned. Free: yt-dlp + tikwm + on-device OCR.

    python3 teardown.py --dir rival tag someapppartner           # who posts under a hashtag
    python3 teardown.py --dir rival pull @acc1 @acc2 --days 200  # history + slide links
    python3 teardown.py --dir rival ocr                          # download + read every slide
    python3 teardown.py --dir rival apps                         # which app names the text carries
    python3 teardown.py --dir rival report --app "some ?app"     # report.md + carousels.json + sheets
    python3 teardown.py --dir rival comments --top 40 --app "some ?app"
    python3 teardown.py --dir rival appstore "some app"          # ratings count, release date

Every step is resumable: re-run it after a crash or a tikwm daily cap and it carries on.
Folder: accounts/ (yt-dlp), posts.json (slide links), slides/, ocr.json, carousels.json,
report.md, sheets/, comments.json, tags/, appstore.json.
"""
import argparse
import collections
import json
import re
import shutil
import statistics as st
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tt  # noqa: E402
from account import fetch  # noqa: E402

CTA = re.compile(r"save (this|it|for)|follow (me|for)|report back|which one|link in bio|let me know"
                 r"|comment (below|your|which|if)|drop a|try one", re.I)


def jload(p, default):
    return json.loads(p.read_text()) if p.exists() else default


# ---------- tag: find the network ----------

def tag(a, D):
    info = tt.tikwm("challenge/info", challenge_name=a.name.lstrip("#"))
    if not info:
        sys.exit("hashtag not found")
    print(f"#{info.get('cha_name')}: {info.get('user_count')} posts, {info.get('view_count')} views (TikTok's totals)")
    posts, cur = {}, 0
    for _ in range(a.pages):
        d = tt.tikwm("challenge/posts", challenge_id=info["id"], count=35, cursor=cur) or {}
        for v in d.get("videos") or []:
            posts[v["video_id"]] = {"id": v["video_id"], "handle": (v.get("author") or {}).get("unique_id"),
                                    "ts": v.get("create_time"), "views": v.get("play_count") or 0,
                                    "photo": bool(v.get("images")), "caption": v.get("title")}
        if not d.get("hasMore") or not d.get("videos"):
            break
        cur = d["cursor"]
    (D / "tags").mkdir(parents=True, exist_ok=True)
    (D / "tags" / f"{info['cha_name']}.json").write_text(json.dumps({"info": info, "posts": list(posts.values())},
                                                                     ensure_ascii=False))
    by = collections.defaultdict(list)
    for p in posts.values():
        by[p["handle"]].append(p)
    print(f"sample {len(posts)} posts, {len(by)} accounts. A tag is not the network: check each with pull + report.")
    print(f"{'account':28} {'posts':>5} {'photo':>5} {'median':>9}")
    for h, ps in sorted(by.items(), key=lambda kv: -len(kv[1]))[:a.show]:
        print(f"{h:28} {len(ps):5} {sum(p['photo'] for p in ps):5} {st.median(p['views'] for p in ps):9,.0f}")


# ---------- pull: history (yt-dlp) + slide links (tikwm) ----------

def pull(a, D):
    (D / "accounts").mkdir(parents=True, exist_ok=True)
    handles = [tt.handle_of(h) for h in a.handles]
    cut = time.time() - a.days * tt.DAY
    db = jload(D / "posts.json", {})
    todo = []
    for h in handles:
        _, entries, err = fetch(h, D / "accounts", a.cap or None)
        if entries is None:
            print(f"  {h}: FAILED {err}")
            continue
        car = sorted((e for e in entries if tt.is_carousel(e) and (e.get("timestamp") or 0) > cut),
                     key=lambda e: -(e.get("timestamp") or 0))[:a.max_posts or None]
        new = [e for e in car if db.get(e["id"], {}).get("images") is None]
        print(f"  {h}: {len(entries)} posts, {len(car)} carousels in {a.days} d, {len(new)} to pull")
        todo += [(h, e) for e in new]
    print(f"{len(todo)} tikwm calls, ~{len(todo) * 1.2 / 60:.0f} min at 1/s (slower when the free API is busy)")

    def one(job):
        h, e = job
        url = e.get("url") or f"https://www.tiktok.com/@{h}/photo/{e['id']}"
        d = tt.tikwm("", url=url) or {}
        return e["id"], {"handle": h, "url": url, "ts": e.get("timestamp"), "views": e.get("view_count") or 0,
                         "likes": e.get("like_count") or 0, "saves": e.get("save_count") or 0,
                         "comments": e.get("comment_count") or 0, "shares": e.get("repost_count") or 0,
                         "desc": e.get("description") or "", "track": e.get("track"),
                         "title": d.get("title") or "", "images": d.get("images") if d else None}
    with ThreadPoolExecutor(4) as ex:  # tt.tikwm paces all threads to 1 request/s
        for n, (pid, row) in enumerate(ex.map(one, todo), 1):
            db[pid] = row
            if n % 20 == 0 or n == len(todo):
                (D / "posts.json").write_text(json.dumps(db, ensure_ascii=False))
                print(f"  {n}/{len(todo)}", flush=True)
    (D / "posts.json").write_text(json.dumps(db, ensure_ascii=False))
    print(f"{sum(1 for p in db.values() if p.get('images'))} carousels with slides in posts.json")


# ---------- ocr ----------

def vision_reader():
    import Vision  # pyobjc-framework-Vision, macOS only
    from Foundation import NSData

    def read(path):
        data = Path(path).read_bytes()
        req = Vision.VNRecognizeTextRequest.alloc().init()
        req.setRecognitionLevel_(0)  # 0 = accurate
        h = Vision.VNImageRequestHandler.alloc().initWithData_options_(NSData.dataWithBytes_length_(data, len(data)), None)
        h.performRequests_error_([req], None)
        lines = []
        for o in sorted(req.results() or [], key=lambda o: -o.boundingBox().origin.y):
            b = o.boundingBox()  # normalised, origin bottom-left -> store top-left
            lines.append([o.topCandidates_(1)[0].string(), round(b.origin.x, 3),
                          round(1 - b.origin.y - b.size.height, 3), round(b.size.width, 3), round(b.size.height, 3)])
        return lines
    return read


def tesseract_reader():
    if not shutil.which("tesseract"):
        raise RuntimeError("tesseract not on PATH")
    from PIL import Image

    def read(path):
        W, H = Image.open(path).size
        tsv = subprocess.run(["tesseract", str(path), "-", "tsv"], capture_output=True, text=True).stdout
        groups = collections.OrderedDict()
        for r in tsv.splitlines()[1:]:
            c = r.split("\t")
            if len(c) == 12 and c[11].strip() and float(c[10]) > 30:
                groups.setdefault(tuple(c[1:5]), []).append((c[11], *map(int, c[6:10])))
        lines = []
        for words in groups.values():
            x0 = min(w[1] for w in words); y0 = min(w[2] for w in words)
            x1 = max(w[1] + w[3] for w in words); y1 = max(w[2] + w[4] for w in words)
            lines.append([" ".join(w[0] for w in words), round(x0 / W, 3), round(y0 / H, 3),
                          round((x1 - x0) / W, 3), round((y1 - y0) / H, 3)])
        return lines
    return read


def ocr(a, D):
    from PIL import Image
    readers = {"vision": vision_reader, "tesseract": tesseract_reader}
    order = [a.engine] if a.engine != "auto" else ["vision", "tesseract"]
    read = None
    for name in order:
        try:
            read = readers[name]()
            print(f"OCR engine: {name}", file=sys.stderr)
            break
        except Exception as e:
            print(f"  {name} unavailable: {e}", file=sys.stderr)
    if not read:
        sys.exit("no OCR engine: macOS -> pip install pyobjc-framework-Vision; else install tesseract")
    db = jload(D / "posts.json", {})
    out = jload(D / "ocr.json", {})
    slides = D / "slides"
    slides.mkdir(exist_ok=True)
    todo = [pid for pid, p in db.items() if p.get("images") and pid not in out]
    print(f"{len(todo)} carousels to read (~{sum(len(db[p]['images']) for p in todo)} slides)")
    for n, pid in enumerate(todo, 1):
        files = [slides / f"{pid}_{k}.jpg" for k in range(1, len(db[pid]["images"]) + 1)]
        with ThreadPoolExecutor(8) as ex:
            list(ex.map(lambda j: _safe(tt.download, *j), zip(db[pid]["images"], files)))
        row = []
        for f in files:
            try:
                w, h = Image.open(f).size
                row.append({"w": w, "h": h, "lines": read(f)})
                if a.delete_slides:
                    f.unlink()
            except Exception as e:
                row.append({"err": str(e)[:80]})  # slide links expire after days: re-pull if many fail
        out[pid] = row
        if n % 10 == 0 or n == len(todo):
            (D / "ocr.json").write_text(json.dumps(out, ensure_ascii=False))
            print(f"  {n}/{len(todo)}", flush=True)


def _safe(fn, *args):
    try:
        return fn(*args)
    except Exception:
        return None


# ---------- table + report ----------

def month(ts):
    return time.strftime("%Y-%m", time.gmtime(ts or 0))


def medians(D, db):
    """(handle, month) -> median views over the account's FULL history (videos too), 5+ posts
    that month; handle -> account median as fallback."""
    views = collections.defaultdict(list)
    for f in (D / "accounts").glob("*.json"):
        for e in json.loads(f.read_text()).get("entries") or []:
            views[(f.stem, month(e.get("timestamp")))].append(e.get("view_count") or 0)
    if not views:
        for p in db.values():
            views[(p["handle"], month(p["ts"]))].append(p["views"])
    acc = collections.defaultdict(list)
    for (h, _), v in views.items():
        acc[h] += v
    return ({k: st.median(v) for k, v in views.items() if len(v) >= 5}, {h: st.median(v) for h, v in acc.items()})


def table(D, app):
    db, ocr_ = jload(D / "posts.json", {}), jload(D / "ocr.json", {})
    by_month, by_acc = medians(D, db)
    rows = []
    for pid, slides in ocr_.items():
        p = db.get(pid)
        ok = [s for s in slides if "w" in s]
        if not p or not ok:
            continue
        text = [" ".join(l[0] for l in s["lines"]) for s in ok]
        m = by_month.get((p["handle"], month(p["ts"]))) or by_acc.get(p["handle"]) or 0
        cap = f"{p.get('title') or ''} {p.get('desc') or ''}"
        rows.append({
            "id": pid, "handle": p["handle"], "url": p["url"], "posted": time.strftime("%Y-%m-%d", time.gmtime(p["ts"] or 0)),
            "views": p["views"], "saves": p["saves"], "idx": round(p["views"] / m, 2) if m else None,
            "n": len(ok), "landscape": sum(s["w"] > s["h"] for s in ok) / len(ok) > .5,
            "cover": text[0], "slides": text, "words": [len(t.split()) for t in text],
            "app_slides": [i + 1 for i, t in enumerate(text) if app and app.search(t)],
            "app_caption": bool(app and app.search(cap)), "cta_last": bool(CTA.search(text[-1])),
            "caption": p.get("title") or "", "track": p.get("track")})
    (D / "carousels.json").write_text(json.dumps(rows, ensure_ascii=False))
    return rows


def line(name, rs):
    v = [r["views"] for r in rs]
    i = [r["idx"] for r in rs if r["idx"] is not None]
    sv = [100 * r["saves"] / max(r["views"], 1) for r in rs]
    beat = 100 * sum(x > 1 for x in i) / len(i) if i else 0
    return (f"| {name[:60]} | {len(rs)} | {st.median(v):,.0f} | {st.median(i) if i else 0:.2f} | {beat:.0f}% | "
            f"{st.median(sv):.2f} |")


HEAD = ["| group | n | median views | median index | beat own month | save % |", "|---|---|---|---|---|---|"]


def group(title, rows, key, min_n=1, limit=None):
    g = collections.defaultdict(list)
    for r in rows:
        g[key(r)].append(r)
    keep = sorted(((k, rs) for k, rs in g.items() if len(rs) >= min_n), key=lambda kv: -len(kv[1]))[:limit]
    return [f"### {title}", ""] + HEAD + [line(str(k), rs) for k, rs in keep] + [""]


def position(r):
    if not r["app_slides"]:
        return "not on a slide"
    s = r["app_slides"][0]
    return "cover" if s == 1 else "last" if s == r["n"] else "2nd-last" if s == r["n"] - 1 else f"slide {s}"


def sentence(text, m):
    start = max(text.rfind(c, 0, m.start()) for c in ".!?\"”") + 1
    ends = [i for i in (text.find(c, m.end()) for c in ".!?") if i > 0]
    return text[start:min(ends) + 1 if ends else None].strip()


def cover_key(t):
    return " ".join(re.sub(r"[^a-z ]", " ", t.lower()).split()[-8:])  # photo OCR junk sits in front


def report(a, D):
    app = re.compile(a.app, re.I) if a.app else None
    rows = table(D, app)
    if not rows:
        sys.exit("nothing read yet: run pull, then ocr")
    md = [f"# Carousel teardown — {len(rows)} carousels, {len({r['handle'] for r in rows})} accounts", "",
          "index = views / median of the same account in the same month (account x month swamps format, so "
          "plain account medians give fake effects). Views are not installs.", ""]

    md += ["## Accounts", "", "| account | carousels | median views | app on a slide | app in caption | looks like |",
           "|---|---|---|---|---|---|"]
    for h, rs in sorted(collections.Counter(r["handle"] for r in rows).items(), key=lambda kv: -kv[1]):
        rs = [r for r in rows if r["handle"] == h]
        on = sum(bool(r["app_slides"]) for r in rs) / len(rs)
        cap = sum(r["app_caption"] for r in rs) / len(rs)
        role = "-" if not app else "partner (app on most slides)" if on > .5 else "caption plug" if cap > .5 else "organic / other"
        md.append(f"| @{h} | {len(rs)} | {st.median(r['views'] for r in rs):,.0f} | {on:.0%} | {cap:.0%} | {role} |")
    md.append("")

    md += ["## How the posts are built", ""]
    md += group("Slide count", rows, lambda r: r["n"])
    md += group("Frame", rows, lambda r: "landscape" if r["landscape"] else "portrait")
    md += group("Cover has a number", rows, lambda r: bool(re.search(r"\b\d+\b", r["cover"][:60])))
    md += group("Last slide is a call to action", rows, lambda r: r["cta_last"])
    md += group("Posted month", rows, lambda r: r["posted"][:7])
    w_cover = [r["words"][0] for r in rows]
    w_body = [w for r in rows for w in r["words"][1:]]
    md += [f"Words: cover median {st.median(w_cover)}, body slide median {st.median(w_body) if w_body else 0}.", ""]

    g = collections.defaultdict(list)
    for r in rows:
        g[cover_key(r["cover"])].append(r)
    reused = {k: v for k, v in g.items() if k and len(v) >= 3}
    n_re = sum(len(v) for v in reused.values())
    md += ["### Cover texts reused 3+ times", "",
           f"{n_re} of {len(rows)} carousels ({n_re / len(rows):.0%}) sit on a cover text used 3+ times; "
           f"{len(g)} distinct covers.", ""] + HEAD
    md += [line(f"{k} ({len({r['handle'] for r in v})} acc)", v) for k, v in
           sorted(reused.items(), key=lambda kv: -len(kv[1]))[:20]] + [""]

    if app:
        md += ["## The app mention", ""]
        md += group("Named on a slide", rows, lambda r: bool(r["app_slides"]))
        md += group("Where the first mention sits", rows, position)
        ms = [(r, r["slides"][r["app_slides"][0] - 1]) for r in rows if r["app_slides"]]
        if ms:
            sents = [(r, sentence(t, app.search(t))) for r, t in ms]
            mw = [len(t.split()) for _, t in ms]
            other = [w for r in rows if r["app_slides"] for i, w in enumerate(r["words"]) if i + 1 not in r["app_slides"]]
            md += [f"Mention slide: median {st.median(mw)} words vs {st.median(other) if other else 0} on the other slides. "
                   f"{len({s.lower() for _, s in sents})} distinct mention sentences of {len(sents)}.", ""]
            tpl = collections.defaultdict(list)
            for r, s in sents:
                tpl[" ".join(re.sub(r"[^a-z ]", "", s.lower()).split()[:6])].append(r)
            md += group("Mention openings (first 6 words), 2+ uses", rows=[dict(r, _k=k) for k, rs in tpl.items() for r in rs],
                        key=lambda r: r["_k"], min_n=2, limit=25)
            md += ["### Top mention sentences by views", ""]
            md += [f"- {r['views']:,} views, slide {r['app_slides'][0]}/{r['n']}: \"{s[:240]}\"" for r, s in
                   sorted(sents, key=lambda x: -x[0]["views"])[:12]] + [""]

    (D / "sheets").mkdir(exist_ok=True)
    top = sorted(rows, key=lambda r: -r["views"])[:a.top]
    md += ["## Top carousels", "", "| views | index | slides | account | cover | sheet |", "|---|---|---|---|---|---|"]
    for r in top:
        files = sorted((D / "slides").glob(f"{r['id']}_*.jpg"), key=lambda f: int(f.stem.split("_")[1]))
        ok = files and tt.sheet(files, D / "sheets" / f"{r['id']}.jpg", title=f"@{r['handle']}  {r['views']:,} views  {r['posted']}")
        md.append(f"| [{r['views']:,}]({r['url']}) | {r['idx']} | {r['n']} | @{r['handle']} | {r['cover'][:70]} | "
                  f"{'sheets/' + r['id'] + '.jpg' if ok else '(slides deleted)'} |")
    (D / "report.md").write_text("\n".join(md) + "\n")
    print(f"{len(rows)} carousels -> {D / 'report.md'}, {D / 'carousels.json'}, {D / 'sheets'}")


# ---------- apps: which app names the text carries ----------

STOP = set("""this the an a my your our their his her its that one any every some no new free best fav favorite favourite
fave good great cool cute only same other first second last which what notes note camera phone tiktok instagram dating
fitness health calendar weather clock photos photo music budget bank journal journaling calorie workout meditation sleep
habit tracker study language reading shopping food recipe planner productivity social period ai chat chatbot game games
editing editor video youtube google apple iphone ios android messages message texting text maps settings mail email
reminders screen time mobile web site website weight skincare beauty makeup fashion outfit mental therapy self care
astrology meal water step steps running walking yoga gym books book podcast news paid premium subscription download
downloaded downloading trial lock home widget wallpaper another these those such whole actual real literally actually
just also even still into using use try when where how why who and or but for with from about on in at to of as by is
was are be been it i you he she we they me him them us dm dms called named travel snapchat whatsapp""".split())

PATTERNS = [
    r"\b(?:app|application)\s+(?:is\s+)?(?:called|named)\s*[:\-]?\s*[\"“']?([A-Za-z0-9][\w'.+\-]*(?:\s+[A-Z0-9][\w'.+\-]*){0,2})",
    r"\b([A-Z0-9][\w'.+\-]*(?:\s+[A-Z0-9][\w'.+\-]*){0,2})\s+app\b",
    r"\b([a-z0-9][\w'.+\-]*)\s+app\b",
    r"\b(?:download|downloaded|downloading)\s+(?:the\s+)?[\"“']?([A-Z0-9][\w'.+\-]*(?:\s+[A-Z0-9][\w'.+\-]*){0,2})",
    r"@([\w.]{3,24})",
    r"#(\w{3,22}?)(?:app|partner|ambassador)\b",
]


def norm(name):
    words = [w for w in re.split(r"\s+", name.strip(" .,:;!?'\"“”-")) if w]
    while words and words[0].lower() in STOP:
        words = words[1:]
    while words and words[-1].lower() in STOP:
        words = words[:-1]
    key = re.sub(r"[^a-z0-9]", "", " ".join(words).lower())
    return key if 3 <= len(key) <= 24 and not key.isdigit() else None


def names_in(text):
    found = {}
    for pat in PATTERNS:
        for m in re.finditer(pat, text, re.I if pat.startswith("#") else 0):
            k = norm(m.group(1))
            if k:
                found.setdefault(k, text[max(0, m.start() - 40):m.end() + 40].replace("\n", " "))
    return found


def apps(a, D):
    db, ocr_ = jload(D / "posts.json", {}), jload(D / "ocr.json", {})
    by = collections.defaultdict(lambda: {"accounts": set(), "posts": 0, "views": 0, "ex": []})
    for pid, p in db.items():
        found = {k: "caption: " + v for k, v in names_in(f"{p.get('title') or ''} {p.get('desc') or ''}").items()}
        for n, s in enumerate(ocr_.get(pid) or [], 1):
            for k, v in names_in(" ".join(l[0] for l in s.get("lines", []))).items():
                found.setdefault(k, f"slide {n}: {v}")
        for k, where in found.items():
            r = by[k]
            r["accounts"].add(p["handle"])
            r["posts"] += 1
            r["views"] += p["views"]
            if len(r["ex"]) < 5:
                r["ex"].append({"url": p["url"], "line": where[:160]})
    rows = sorted(({"name": k, "accounts": sorted(v["accounts"]), "posts": v["posts"], "views": v["views"], "ex": v["ex"]}
                   for k, v in by.items()), key=lambda r: (-len(r["accounts"]), -r["posts"]))
    (D / "apps.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1))
    print("candidates (a name counts once per post; check the lines — creator tags like #xpartner are the strongest signal)")
    for r in rows[:a.show]:
        print(f"  {r['name'][:22]:22} acc {len(r['accounts']):3} posts {r['posts']:4}  {r['ex'][0]['line'][:90]}")


# ---------- comments ----------

ASK = re.compile(r"what(?:'s| is) (?:the |this )?app|which app|is it free|how much|android|name of|link\??$|where", re.I)


def comments(a, D):
    db = jload(D / "posts.json", {})
    out = jload(D / "comments.json", {})
    app = re.compile(a.app, re.I) if a.app else None
    top = sorted((kv for kv in db.items() if kv[1].get("images")), key=lambda kv: -kv[1]["views"])[:a.top]
    for pid, p in top:
        if pid not in out:
            d = tt.tikwm("comment/list", url=p["url"], count=50, cursor=0) or {}
            out[pid] = [[c.get("text"), c.get("digg_count"), (c.get("user") or {}).get("unique_id")]
                        for c in d.get("comments") or []]
            (D / "comments.json").write_text(json.dumps(out, ensure_ascii=False))
    studied = {p["handle"] for p in db.values()}
    allc = [(pid, *c) for pid, cs in out.items() for c in cs]
    print(f"{len(allc)} comments on {len(out)} carousels (first page, up to 50 each)")
    if app:
        named = [c for c in allc if app.search(c[1] or "")]
        print(f"  name the app: {len(named)}")
    asks = [c for c in allc if ASK.search(c[1] or "")]
    print(f"  ask about the app / price / platform: {len(asks)}")
    seeded = [c for c in allc if c[3] in studied and c[3] != db[c[0]]["handle"]]
    print(f"  written by OTHER studied accounts (comment seeding?): {len(seeded)}")
    for c in seeded[:15]:
        print(f"    @{c[3]} on @{db[c[0]]['handle']}: {(c[1] or '')[:90]}")


# ---------- appstore ----------

def appstore(a, D):
    """US App Store facts, free (iTunes Search API, ~20 calls/min)."""
    rows = []
    for term in a.terms:
        url = "https://itunes.apple.com/search?" + urllib.parse.urlencode(
            {"term": term, "entity": "software", "country": a.country, "limit": 5})
        res = json.loads(urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}),
                                                timeout=30).read()).get("results", [])
        for r in res[:3]:
            rows.append({"term": term, "id": r.get("trackId"), "name": r.get("trackName"), "seller": r.get("sellerName"),
                         "ratings": r.get("userRatingCount"), "stars": round(r.get("averageUserRating") or 0, 2),
                         "released": (r.get("releaseDate") or "")[:10], "updated": (r.get("currentVersionReleaseDate") or "")[:10],
                         "genre": r.get("primaryGenreName"), "url": (r.get("trackViewUrl") or "").split("?")[0]})
            print(f"  {term[:18]:18} | {rows[-1]['name'][:38]:38} | {rows[-1]['ratings'] or 0:>8} ratings | "
                  f"{rows[-1]['released']} | {rows[-1]['url']}")
        time.sleep(3.2)
    old = jload(D / "appstore.json", [])
    (D / "appstore.json").write_text(json.dumps(old + [dict(r, checked=time.strftime("%Y-%m-%d")) for r in rows], indent=1))
    print("ratings count is exact for that country; ratings x 50-150 as installs is a rough rule of thumb, not data."
          " Re-check in a few days: the ratings delta per day is the cleanest public growth signal.")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", default="teardown", help="study folder (default ./teardown)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("tag", help="sample a hashtag's posts and authors (tikwm, free)")
    s.add_argument("name")
    s.add_argument("--pages", type=int, default=10, help="35 posts a page, 1 call each")
    s.add_argument("--show", type=int, default=30)
    s = sub.add_parser("pull", help="history (yt-dlp) + slide links (tikwm) for accounts")
    s.add_argument("handles", nargs="+")
    s.add_argument("--days", type=int, default=200, help="carousels newer than this")
    s.add_argument("--cap", type=int, default=600, help="newest posts read per account (0 = all)")
    s.add_argument("--max-posts", type=int, default=0, help="newest N carousels per account (0 = all)")
    s = sub.add_parser("ocr", help="download slides and read their text with boxes")
    s.add_argument("--engine", choices=["auto", "vision", "tesseract"], default="auto")
    s.add_argument("--delete-slides", action="store_true", help="drop images after reading (~0.3 MB a slide); no sheets then")
    s = sub.add_parser("report", help="report.md + carousels.json + sheets/")
    s.add_argument("--app", help="regex for the app studied, app-specific (a topic word matches the topic, not the plug)")
    s.add_argument("--top", type=int, default=12, help="top carousels with sheets")
    s = sub.add_parser("apps", help="candidate app names in captions + slides")
    s.add_argument("--show", type=int, default=40)
    s = sub.add_parser("comments", help="first 50 comments of the top carousels (tikwm)")
    s.add_argument("--top", type=int, default=40)
    s.add_argument("--app", help="regex for the app")
    s = sub.add_parser("appstore", help="App Store facts for app names")
    s.add_argument("terms", nargs="+")
    s.add_argument("--country", default="us")
    a = ap.parse_args()
    D = Path(a.dir)
    D.mkdir(parents=True, exist_ok=True)
    {"tag": tag, "pull": pull, "ocr": ocr, "report": report, "apps": apps, "comments": comments,
     "appstore": appstore}[a.cmd](a, D)


if __name__ == "__main__":
    main()
