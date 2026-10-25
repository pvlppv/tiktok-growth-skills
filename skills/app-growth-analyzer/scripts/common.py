"""Shared bits for every script: .env, the data folder, the accounts file."""

import os
import sys
from pathlib import Path


def load_env(path=None):
    """KEY=value lines from .env into os.environ. A variable already set wins."""
    for p in [Path(path)] if path else [Path.cwd() / ".env", Path(__file__).resolve().parent.parent / ".env"]:
        if p.is_file():
            for line in p.read_text().splitlines():
                key, sep, value = line.strip().partition("=")
                if sep and not key.startswith("#"):
                    os.environ.setdefault(key.strip(), value.strip().strip("'\""))
            return


def data_dir(arg=None):
    """where caches, snapshots and posts.csv live: --data, else GROWTH_DATA, else ./growth-data."""
    d = Path(arg or os.environ.get("GROWTH_DATA") or "growth-data").expanduser()
    d.mkdir(parents=True, exist_ok=True)
    return d


def accounts(path=None):
    """[(handle, label, tiktok_id)] from the accounts file: one account per line,
    `handle [label words] [id:<tiktok user id>]`. `#` starts a comment.
    id: is only needed when TikTok refuses to read a profile by its name."""
    p = Path(path or os.environ.get("ACCOUNTS_FILE") or "accounts.txt").expanduser()
    if not p.is_file():
        return []
    out = []
    for line in p.read_text().splitlines():
        words = line.split("#")[0].split()
        if not words:
            continue
        uid = next((w[3:] for w in words[1:] if w.startswith("id:")), None)
        label = " ".join(w for w in words[1:] if not w.startswith("id:"))
        out.append((words[0].lstrip("@"), label, uid))
    return out


def need(*keys):
    missing = [k for k in keys if not os.environ.get(k)]
    if missing:
        sys.exit(f"missing {', '.join(missing)} — put them in .env (see .env.example)")
