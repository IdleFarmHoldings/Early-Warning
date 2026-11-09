"""Shared helpers: HTTP, JSON files, time, ntfy push."""
import json
import os
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG = json.loads((ROOT / "collector" / "config.json").read_text())
UA = CONFIG["user_agent"]


def now_utc():
    return datetime.now(timezone.utc)


def iso(dt=None):
    return (dt or now_utc()).strftime("%Y-%m-%dT%H:%M:%SZ")


def http_get(url, timeout=25, accept="*/*"):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": accept})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def get_json(url, **kw):
    return json.loads(http_get(url, accept="application/json", **kw).decode("utf-8", "replace"))


def load_json(path, default):
    p = ROOT / path
    try:
        return json.loads(p.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path, data):
    p = ROOT / path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n")


def append_jsonl(path, row):
    p = ROOT / path
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a") as f:
        f.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def in_box(lat, lon, box):
    lat_min, lat_max, lon_min, lon_max = box
    if lat is None or lon is None:
        return False
    if lon < 0 and lon_max > 180:  # boxes may run past 180 E
        lon += 360
    return lat_min <= lat <= lat_max and lon_min <= lon <= lon_max


def _ascii(s):
    return s.encode("ascii", "ignore").decode()


def push(channel, title, message, priority=3, tags=None, click=None, dry_run=False):
    """Send to ntfy. channel is 'proactive' or 'reactive'. Returns True if sent."""
    topic = os.environ.get("NTFY_PROACTIVE" if channel == "proactive" else "NTFY_REACTIVE", "").strip()
    if dry_run or not topic:
        print(f"[push skipped: {'dry run' if dry_run else 'no topic'}] {channel}: {title} | {message}")
        return False
    headers = {"User-Agent": UA, "Title": _ascii(title)[:200], "Priority": str(priority)}
    if tags:
        headers["Tags"] = ",".join(tags)
    if click:
        headers["Click"] = click
    req = urllib.request.Request(f"https://ntfy.sh/{topic}", data=message.encode("utf-8"),
                                 headers=headers, method="POST")
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=20):
                return True
        except (urllib.error.URLError, TimeoutError) as e:
            print(f"ntfy attempt {attempt + 1} failed: {e}")
            time.sleep(3)
    return False
