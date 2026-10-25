#!/usr/bin/env python3
"""tiktok-auto-poster: deal finished carousels across accounts, get an OK in Telegram, then post.

A carousel is one folder of images (sorted by name = slide order) plus an optional caption.txt:
first paragraph = TikTok title, the rest = description / hashtags.

    poster.py plan    --dir out/a --dir out/b            # print today's deal, change nothing
    poster.py plan    --dir out/a --dir out/b --save     # write the deal into each folder (.plan)
    poster.py hold    --dir out/a --new                  # put every new folder on hold
    poster.py review  --dir out/a --dir out/b --live     # held ones -> Telegram for an OK
    poster.py approve --dir out/a --all                  # lift the hold on what was reviewed
    poster.py telegram --dir ... --live                  # albums for accounts posted by hand
    poster.py postiz  --dir ... --live                   # TikTok inbox drafts via Postiz Cloud
    poster.py farm    --dir ... --live                   # one-shot tasks on an iPhone farm
    poster.py check                                      # Postiz posts of the last 2 days

Everything that sends, books or pushes is a dry run unless --live is given.
Markers inside each carousel folder make every step safe to re-run:
    .hold      waiting for an OK — nothing posts it        .approved  hold lifted
    .plan      today's account (json)                      .reviewed  review album sent
    .sent      Telegram album sent    .postiz  draft booked    .farm  farm task created
"""

import argparse
import html
import json
import os
import sys
import time
from collections import Counter, namedtuple
from datetime import date, datetime
from pathlib import Path

import requests

SKILL = Path(__file__).resolve().parent.parent
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp"}
DONE = {"telegram": ".sent", "postiz": ".postiz", "farm": ".farm"}
ROUTES = tuple(DONE)
HOLD, APPROVED, REVIEWED, PLAN = ".hold", ".approved", ".reviewed", ".plan"
TG_API = "https://api.telegram.org/bot{token}/{method}"
CAPTION_LIMIT = 1024          # Telegram caption cap
ALBUM_MAX = 10                # sendMediaGroup takes 2-10 items

Account = namedtuple("Account", "handle device route")


# ---------- env, accounts, carousels -------------------------------------------------------

def load_env(path=None):
    """read KEY=VALUE lines into os.environ. A scheduled run (cron, a routine) does not source
    your shell profile, so keys live in a file. A real env var wins over the file, and an empty
    value never wins, so a blank `TOKEN=` left from .env.example cannot beat a real token."""
    if path and not Path(path).is_file():
        sys.exit(f"no such env file: {path}")
    for p in ([Path(path)] if path else [Path.cwd() / ".env", SKILL / ".env"]):
        if not p.is_file():
            continue
        for line in p.read_text().splitlines():
            line = line.strip().removeprefix("export ").strip()
            if not line or line.startswith("#"):
                continue
            key, sep, val = line.partition("=")
            key, val = key.strip(), val.strip().strip("'\"")
            if sep and val and not os.environ.get(key):
                os.environ[key] = val
        return


def load_accounts(path):
    """[Account] in file order. Line: `handle  device name words  [telegram|postiz|farm]`."""
    out = []
    for n, raw in enumerate(Path(path).read_text().splitlines(), 1):
        words = raw.split("#", 1)[0].split()
        if not words:
            continue
        route = words.pop() if words[-1] in ROUTES and len(words) > 2 else "telegram"
        if len(words) < 2:
            sys.exit(f"{path}:{n}: need `handle device [route]`, got: {raw.strip()}")
        out.append(Account(words[0].lstrip("@"), " ".join(words[1:]), route))
    handles = [a.handle for a in out]
    dupes = [h for h, c in Counter(handles).items() if c > 1]
    if dupes:
        sys.exit(f"{path}: handle listed twice: {', '.join(dupes)}")
    if not out:
        sys.exit(f"{path}: no accounts")
    return out


def images(d):
    return sorted(p for p in d.iterdir() if p.suffix.lower() in IMAGE_EXT)


def carousels(root):
    root = Path(root)
    if not root.is_dir():
        sys.exit(f"no such folder: {root}")
    return sorted(d for d in root.iterdir() if d.is_dir() and images(d))


def caption(d):
    """(title, description) from caption.txt: first paragraph, then the rest."""
    f = d / "caption.txt"
    paras = [p.strip() for p in f.read_text().split("\n\n") if p.strip()] if f.exists() else []
    return (paras[0], "\n\n".join(paras[1:])) if paras else ("", "")


def done(d):
    return any((d / m).exists() for m in DONE.values())


def read_plan(d):
    try:
        return json.loads((d / PLAN).read_text())
    except (OSError, ValueError):
        return None


def stamp(d, marker, text=None):
    (d / marker).write_text(text if text is not None else f"{datetime.now():%Y-%m-%d %H:%M:%S}\n")


# ---------- the deal -----------------------------------------------------------------------

def deal(pools, accounts, per_account, taken):
    """[(Account, folder)]: hand every account `per_account` carousels, fairly.

    - devices are dealt in file order, accounts in file order inside a device
    - the folder (format) each slot draws from rotates by device, account and round, so a
      device posts a mix (a b a b) and never a block of one format (a a b b)
    - the opener rotates instead of being random, so a dry run prints exactly what --live does
    - when a folder runs dry its slots are filled from the others; nothing is stranded
    `taken` = {handle: posts already planned today}, so re-running only fills empty slots."""
    queues, nf = [list(p) for p in pools], len(pools)
    devices = list(dict.fromkeys(a.device for a in accounts))
    out = []
    for r in range(per_account):
        for di, dev in enumerate(devices):
            for ai, acc in enumerate(a for a in accounts if a.device == dev):
                if taken.get(acc.handle, 0) > r:
                    continue
                k = (di + ai + r) % nf
                q = next((queues[(k + j) % nf] for j in range(nf) if queues[(k + j) % nf]), None)
                if q is None:
                    return out
                out.append((acc, q.pop(0)))
    return out


def todays_plan(dirs, day):
    """[(plan dict, folder)] already written for `day`, in dealing order."""
    rows = [(p, d) for root in dirs for d in carousels(root)
            if (p := read_plan(d)) and p.get("date") == day]
    return sorted(rows, key=lambda r: r[0].get("seq", 0))


# ---------- Telegram -----------------------------------------------------------------------

def tg(method, data=None, files=None, tries=5):
    """one Bot API call with 429 backoff and retry on a dropped link."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN") or sys.exit("TELEGRAM_BOT_TOKEN missing in .env")
    for attempt in range(tries):
        # a retry re-posts the SAME open handles, already read to EOF by the dead attempt.
        # without the rewind the retry uploads empty files and Telegram accepts them.
        for fh in (files or {}).values():
            fh.seek(0)
        try:
            r = requests.post(TG_API.format(token=token, method=method),
                              data=data, files=files, timeout=120)
            payload = r.json()
        except (requests.exceptions.RequestException, ValueError) as e:
            print(f"  link dropped ({type(e).__name__}), retry")
            time.sleep(min(2 ** attempt, 30))
            continue
        if payload.get("ok"):
            return payload["result"]
        if r.status_code == 429:
            time.sleep(payload.get("parameters", {}).get("retry_after", 5) + 1)
            continue
        raise RuntimeError(f"telegram {method}: {payload.get('description')}")
    raise RuntimeError(f"telegram {method}: failed after {tries} tries")


def destination(kind):
    """ONE chat per command, never a list: a version that mirrored a send to two chats put
    every carousel in both, and the duplicates had to be deleted by hand."""
    chat = (os.environ.get("TELEGRAM_REVIEW_CHAT_ID") if kind == "review" else None) \
        or os.environ.get("TELEGRAM_CHAT_ID")
    if not chat:
        sys.exit("TELEGRAM_CHAT_ID missing in .env")
    return {"chat_id": chat, **({"message_thread_id": t} if (t := os.environ.get("TELEGRAM_TOPIC_ID")) else {})}


def tg_text(dest, text):
    tg("sendMessage", data={**dest, "text": text})
    time.sleep(1)


def tg_album(dest, d):
    """the images as documents (no Telegram recompression), caption under the last one, each
    paragraph its own <code> span so one tap copies the title and one tap the hashtags."""
    title, rest = caption(d)
    text = "\n\n".join(f"<code>{html.escape(p, quote=False)}</code>" for p in (title, rest) if p)
    pics = images(d)
    chunks = [pics[i:i + ALBUM_MAX] for i in range(0, len(pics), ALBUM_MAX)]
    for ci, chunk in enumerate(chunks):
        last = ci == len(chunks) - 1
        handles = {f"f{n}": p.open("rb") for n, p in enumerate(chunk)}
        try:
            if len(chunk) == 1:          # an album needs 2+, a lone file goes on its own
                extra = {"caption": text[:CAPTION_LIMIT], "parse_mode": "HTML"} if last and text else {}
                tg("sendDocument", data={**dest, **extra}, files={"document": handles["f0"]})
                continue
            media = [{"type": "document", "media": f"attach://{k}"} for k in handles]
            if last and text:
                media[-1].update(caption=text[:CAPTION_LIMIT], parse_mode="HTML")
            tg("sendMediaGroup", data={**dest, "media": json.dumps(media)}, files=handles)
        finally:
            for fh in handles.values():
                fh.close()


# ---------- commands -----------------------------------------------------------------------

def rows_for(args, route=None):
    rows = todays_plan(args.dir, args.date)
    if not rows:
        sys.exit(f"nothing planned for {args.date} — run: poster.py plan --save ...")
    return [r for r in rows if route is None or r[0]["route"] == route]


def ready(rows, route):
    """the rows this route may act on now: not held, not already done. prints what it skips."""
    out = []
    for p, d in rows:
        if (d / HOLD).exists():
            print(f"  held, skipped: {d.name}  (@{p['handle']})")
        elif (d / DONE[route]).exists():
            print(f"  already done:  {d.name}  (@{p['handle']})")
        else:
            out.append((p, d))
    return out


def status(d):
    for m, word in ((".sent", "sent"), (".postiz", "booked"), (".farm", "pushed"), (HOLD, "HELD")):
        if (d / m).exists():
            return word
    return "ready"


def cmd_plan(args):
    accounts = load_accounts(args.accounts)
    existing = todays_plan(args.dir, args.date)
    taken = Counter(p["handle"] for p, _ in existing)
    planned = {d for _, d in existing}
    pools = [[d for d in carousels(r) if not done(d) and d not in planned] for r in args.dir]
    new = deal(pools, accounts, args.per_account, taken)
    seq = max((p.get("seq", 0) for p, _ in existing), default=0)
    rows = list(existing)
    for acc, d in new:
        seq += 1
        p = {"date": args.date, "seq": seq, **acc._asdict()}
        rows.append((p, d))
        if args.save:
            stamp(d, PLAN, json.dumps(p) + "\n")
    rows.sort(key=lambda r: r[0]["seq"])

    print(f"{args.date}: {len(rows)} post(s) on {len(accounts)} account(s)"
          f" ({len(existing)} already planned, {len(new)} new)")
    print(f"  {'device':14} {'account':22} {'route':9} {'state':7} carousel / title")
    for p, d in rows:
        print(f"  {p['device'][:14]:14} @{p['handle'][:21]:21} {p['route']:9} {status(d):7} "
              f"{d.parent.name}/{d.name}  {caption(d)[0][:40]}")
    left = sum(len(q) for q in pools) - len(new)
    if left:
        print(f"{left} carousel(s) left in the pool for later days")
    empty = [a.handle for a in accounts if Counter(p['handle'] for p, _ in rows)[a.handle] < args.per_account]
    if empty:
        print(f"short: {len(empty)} account(s) got fewer than {args.per_account}: {', '.join(empty)}")
    if new and not args.save:
        print("preview only — add --save to write this deal")


def cmd_hold(args):
    n = 0
    for root in args.dir:
        for d in carousels(root):
            pick = d.name in (args.ids or []) or (args.new and not done(d)
                   and not (d / APPROVED).exists() and not (d / HOLD).exists())
            if pick and not (d / HOLD).exists():
                stamp(d, HOLD)
                n += 1
    print(f"held {n} carousel(s)")


def cmd_approve(args):
    if not args.ids and not args.all:
        sys.exit("say which: --ids a b c, or --all")
    n, unseen = 0, []
    for root in args.dir:
        for d in carousels(root):
            if not (d / HOLD).exists() or (args.ids and d.name not in args.ids):
                continue
            # never approve what nobody looked at: --all only lifts holds that were reviewed
            if args.all and not args.unseen and not (d / REVIEWED).exists():
                unseen.append(d.name)
                continue
            (d / HOLD).unlink()
            stamp(d, APPROVED)
            n += 1
            print(f"  approved {d.name}")
    if unseen:
        print(f"still held, never reviewed ({len(unseen)}): {', '.join(unseen)} — run review first")
    print(f"approved {n}")


def cmd_review(args):
    todo = [(p, d) for p, d in rows_for(args) if (d / HOLD).exists() and not (d / REVIEWED).exists()]
    if not todo:
        print("nothing waiting for review")
        return
    dest = None if not args.live else destination("review")
    for i, (p, d) in enumerate(todo, 1):
        label = f"REVIEW {i}/{len(todo)} · {p['device']} · @{p['handle']} · {d.name}"
        if not args.live:
            print(f"  [dry] {label}  ({len(images(d))} images)")
            continue
        tg_text(dest, label)
        tg_album(dest, d)
        stamp(d, REVIEWED)
        print(f"  sent {label}")
        time.sleep(args.pause)
    if args.live:
        tg_text(dest, f"{len(todo)} carousel(s) above. Reply OK, or name the ones to fix.")
    print(f"{len(todo)} for review" + ("" if args.live else " [dry run — add --live to send]"))


def cmd_telegram(args):
    todo = ready(rows_for(args, "telegram"), "telegram")
    dest = None if not args.live else destination("post")
    for p, d in todo:
        label = f"{p['device']}, @{p['handle']}:"
        if not args.live:
            print(f"  [dry] {label} {d.name} ({len(images(d))} images)")
            continue
        tg_text(dest, label)
        tg_album(dest, d)
        stamp(d, DONE["telegram"])          # only after the label AND the album went through
        print(f"  sent {d.name} -> @{p['handle']}")
        time.sleep(args.pause)
    print(f"{len(todo)} album(s)" + ("" if args.live else " [dry run — add --live to send]"))


def cmd_postiz(args):
    import postiz
    todo = ready(rows_for(args, "postiz"), "postiz")
    if not todo:
        print("nothing to book")
        return
    day = date.fromisoformat(args.date)
    chans, failed = (postiz.channels() if args.live else None), 0
    for p, d in todo:
        when = postiz.slot(day, args.at, args.tz)
        if not args.live:
            print(f"  [dry] {d.name} -> @{p['handle']} draft {when:%Y-%m-%d %H:%M %Z}")
            continue
        try:
            res = postiz.book_draft(p["handle"], d, when, chans)
        except Exception as e:                   # one bad account never stops the others
            failed += 1
            print(f"  POSTIZ FAILED @{p['handle']} {d.name}: {e}")
            continue
        stamp(d, DONE["postiz"], json.dumps({"handle": p["handle"], "at": when.isoformat(),
                                             "response": res}, indent=1))
        print(f"  booked {d.name} -> @{p['handle']} {when:%H:%M %Z}")
    print(f"{len(todo) - failed} booked, {failed} failed" if args.live
          else f"{len(todo)} to book [dry run — add --live]")
    if failed:
        sys.exit(1)


def cmd_farm(args):
    import farm
    todo = ready(rows_for(args, "farm"), "farm")[: args.limit or None]
    if not todo:
        print("nothing to push")
        return
    farm.push(todo, args)


def cmd_check(args):
    import postiz
    postiz.check(args.days)


def main():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--dir", action="append", required=True, type=Path, metavar="DIR",
                        help="folder of carousel folders; repeat for several formats")
    common.add_argument("--date", default=date.today().isoformat(), help="plan day (default today)")
    common.add_argument("--env", help="path to .env (default ./.env, then the skill folder)")
    live = argparse.ArgumentParser(add_help=False)
    live.add_argument("--live", action="store_true", help="really do it (default: dry run)")
    live.add_argument("--pause", type=float, default=3.0, help="seconds between Telegram albums")

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("plan", parents=[common], help="deal carousels to accounts (preview unless --save)")
    s.add_argument("--accounts", default="accounts.txt", help="accounts file (see accounts.example.txt)")
    s.add_argument("--per-account", type=int, default=1, help="posts per account per day (default 1)")
    s.add_argument("--save", action="store_true", help="write the deal into each folder (.plan)")
    s = sub.add_parser("hold", parents=[common], help="put carousels on hold until approved")
    s.add_argument("--ids", nargs="*", help="these carousel folder names")
    s.add_argument("--new", action="store_true", help="every folder never held, approved or posted")
    s = sub.add_parser("approve", parents=[common], help="lift the hold")
    s.add_argument("--ids", nargs="*", help="these carousel folder names")
    s.add_argument("--all", action="store_true", help="every held carousel that was reviewed")
    s.add_argument("--unseen", action="store_true", help="with --all: also ones never reviewed")
    sub.add_parser("review", parents=[common, live], help="send today's held carousels to Telegram for an OK")
    sub.add_parser("telegram", parents=[common, live], help="albums for hand-posted accounts")
    s = sub.add_parser("postiz", parents=[common, live], help="book TikTok inbox drafts via Postiz Cloud")
    s.add_argument("--at", default="12:30", help="HH:MM the drafts land (default 12:30)")
    s.add_argument("--tz", default="America/New_York", help="time zone of --at (your audience's)")
    s = sub.add_parser("farm", parents=[common, live], help="one-shot tasks on an iPhone farm")
    s.add_argument("--start", default="20:30", help="first post HH:MM local (default 20:30)")
    s.add_argument("--gap", type=int, default=25, help="minutes between posts on one device")
    s.add_argument("--jitter", type=int, default=6, help="± minutes of seeded jitter per post")
    s.add_argument("--music", type=Path, help="json list of TikTok sound URLs to pick from")
    s.add_argument("--publish", action="store_true", help="post publicly (default: TikTok drafts)")
    s.add_argument("--limit", type=int, default=0, help="push at most N (use 1 on a first run)")
    s = sub.add_parser("check", help="list Postiz posts of the last days, failures first")
    s.add_argument("--days", type=int, default=2)
    s.add_argument("--env", help="path to .env")
    args = ap.parse_args()

    load_env(args.env)
    {"plan": cmd_plan, "hold": cmd_hold, "approve": cmd_approve, "review": cmd_review,
     "telegram": cmd_telegram, "postiz": cmd_postiz, "farm": cmd_farm, "check": cmd_check}[args.cmd](args)


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    main()
