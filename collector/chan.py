"""4chan chatter: a tip-off source, never evidence on its own.

The idea (Palmer Luckey): anonymous posters on 4chan sometimes talk about events hours before the news does.
Most of what is posted there is noise, rumour or worse, so this module only counts activity and spots bursts:
several NEW threads on one topic in a few hours, well above what is normal for that hour of day.

What it does with a burst: lists it in data/brief.json for the twice-daily analysis, which may spend a search
to check it against real sources. It never sends a phone alert and never feeds the watch list. Only thread
titles from bursting topics are kept (short, for the analysis to judge); everything else is just counts.

Uses 4chan's public read-only JSON API (https://github.com/4chan/4chan-API): one catalog per board, at most
one request per second, about every 15 minutes.
"""
import html
import re
import time
from datetime import timedelta

from common import CONFIG, get_json, now_utc

API = "https://a.4cdn.org/{board}/catalog.json"
THREAD_URL = "https://boards.4chan.org/{board}/thread/{no}"
CFG = CONFIG.get("chan", {})

TAG_RE = re.compile(r"<[^>]+>")
# Long-running "general" threads (e.g. "/ukr/ Ukraine war general #1234") are always busy; skip them.
GENERAL_RE = re.compile(r"\bgeneral\b|^\s*/\w{2,8}/", re.I)


def _words_re(words):
    return re.compile(r"\b(?:" + "|".join(re.escape(w).replace(r"\ ", r"\s+") for w in words) + r")\b", re.I)


TOPICS = [{**t, "re": _words_re(t["words"])} for t in CFG.get("topics", [])]
BREAKING_RE = _words_re(CFG.get("breaking_words", ["happening", "breaking"]))


def clean(s):
    """Plain text from a 4chan subject/comment (HTML, quote links and entities removed)."""
    s = (s or "").replace("<br>", " ")
    s = html.unescape(TAG_RE.sub(" ", s))
    s = re.sub(r">>\d+", " ", s)  # reply links
    return re.sub(r"\s+", " ", s).strip()


def parse_catalog(data, board):
    """Threads from a catalog.json payload: [{board, no, title, text, created, replies}]. Stickies skipped."""
    out = []
    for page in data or []:
        for t in (page or {}).get("threads") or []:
            if t.get("sticky") or not t.get("no") or not t.get("time"):
                continue
            sub, com = clean(t.get("sub")), clean(t.get("com"))
            out.append({"board": board, "no": t["no"], "title": (sub or com)[:100],
                        "text": f"{sub} {com[:300]}".strip(), "created": t["time"], "replies": t.get("replies") or 0,
                        "general": bool(GENERAL_RE.search(sub or com[:80]))})
    return out


def topics_of(text):
    return [t["key"] for t in TOPICS if t["re"].search(text or "")]


def aggregate(threads, now):
    """Per topic: new threads (not generals, not dead) started in the last window_hours, and how many use
    breaking-news words. Returns {key: {name, new_threads, breaking, replies, threads: [...]}}."""
    window = CFG.get("window_hours", 3)
    min_replies = CFG.get("min_replies", 15)
    since = (now - timedelta(hours=window)).timestamp()
    out = {t["key"]: {"name": t["name"], "new_threads": 0, "breaking": 0, "replies": 0, "threads": []} for t in TOPICS}
    for th in threads:
        if th["general"] or th["created"] < since or th["replies"] < min_replies:
            continue
        brk = bool(BREAKING_RE.search(th["text"]))
        age_h = max((now.timestamp() - th["created"]) / 3600, 0.1)
        for key in topics_of(th["text"]):
            o = out[key]
            o["new_threads"] += 1
            o["breaking"] += brk
            o["replies"] += th["replies"]
            o["threads"].append({"board": th["board"], "title": th["title"], "replies": th["replies"],
                                 "age_h": round(age_h, 1), "per_hour": round(th["replies"] / age_h),
                                 "breaking": brk, "url": THREAD_URL.format(board=th["board"], no=th["no"])})
    for o in out.values():
        o["threads"].sort(key=lambda x: -x["per_hour"])
    return out


def fetch_chan():
    """Catalogs of the watched boards, aggregated by topic. Each board fails on its own."""
    threads, errors, boards = [], [], []
    for i, board in enumerate(CFG.get("boards", ["pol", "k", "news"])):
        if i:
            time.sleep(1.1)  # API rule: no more than one request per second
        try:
            threads += parse_catalog(get_json(API.format(board=board), timeout=20), board)
            boards.append(board)
        except Exception as e:  # noqa: BLE001
            errors.append(f"/{board}/: {type(e).__name__}: {e}"[:160])
    if not boards:
        raise RuntimeError("; ".join(errors[:3]) or "no boards configured")
    return {"boards": boards, "threads_seen": len(threads), "topics": aggregate(threads, now_utc()), "errors": errors}


def metrics(chan):
    """Series columns (chan_<topic> = new threads in the window) so a normal level can be learned per hour of day."""
    return {f"chan_{k}": v["new_threads"] for k, v in ((chan or {}).get("topics") or {}).items()}


def assess(chan, series, now):
    """Mark each topic BURST / normal / learning baseline. A burst needs at least min_threads new threads and
    either well above the usual level for this hour (z and ratio), or several threads using breaking-news words.
    Thread titles are kept only for bursts."""
    from rules import baseline
    if not chan:
        return None
    floor = CFG.get("min_threads", 3)
    z_min, ratio_min = CFG.get("z_threshold", 3.0), CFG.get("min_ratio", 2.0)
    out = {}
    for key, o in (chan.get("topics") or {}).items():
        n = o["new_threads"]
        b = baseline(series, f"chan_{key}", now)
        row = {"name": o["name"], "new_threads": n, "breaking": o["breaking"], "replies": o["replies"]}
        if b:
            z = (n - b["median"]) / b["spread"]
            row.update({"usual": round(b["median"], 1), "z": round(z, 1)})
            high = z >= z_min and (b["median"] == 0 or n / b["median"] >= ratio_min)
        else:
            high = False
        brk = o["breaking"] >= CFG.get("min_breaking", 2)
        row["status"] = "BURST" if n >= floor and (high or brk) else "normal" if b else "learning baseline"
        if row["status"] == "BURST":
            row["why"] = ("well above usual for this hour" if high else "") + \
                         (" and " if high and brk else "") + (f"{o['breaking']} threads say it is happening now" if brk else "")
            row["threads"] = o["threads"][:CFG.get("keep_threads", 4)]
        out[key] = row
    return {"boards": chan.get("boards"), "threads_seen": chan.get("threads_seen"),
            "window_hours": CFG.get("window_hours", 3), "topics": out,
            "note": "4chan chatter: anonymous and unreliable. A tip-off to check, never evidence; never alerts phones."}
