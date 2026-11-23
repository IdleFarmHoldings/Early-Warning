"""First-hand sources: the providers' own status pages, and the UK maritime agency (UKMTO).

These report an outage or a ship attack before the news does (often 30-60 minutes for an outage, hours for
an attack at sea), and they are the people who actually know.

Status pages: Cloudflare (and any other Atlassian Statuspage in config), Google Cloud, AWS and Azure. Active
incidents are listed for the analysis, and a BIG one (the provider's own top severity) joins incident triage
as an official report, so it can push the usual one-off "outage worldwide" Warning without waiting for three
news outlets. Small incidents (one product, one region, degraded performance) stay on the board.

UKMTO: its site format has not been seen from here yet, so for now this only probes the pages and keeps a
short sample in data/probe/ for writing a proper parser. It never alerts.
"""
import json
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

from common import CONFIG, DATA_ROOT, get_json, http_get, iso, now_utc

CFG = CONFIG.get("firsthand", {})

GCP = "https://status.cloud.google.com/incidents.json"
AWS = "https://status.aws.amazon.com/rss/all.rss"
AZURE = "https://azure.status.microsoft/en-us/status/feed/"
STATUSPAGES = CFG.get("statuspages", [{"name": "Cloudflare", "url": "https://www.cloudflarestatus.com"}])
AU_REGIONS = re.compile(r"\b(ap-southeast-[24]|australia\w*|sydney|melbourne)\b", re.I)


def _dt(s):
    """ISO 8601 or RFC 822 date -> aware datetime, or None."""
    if not s:
        return None
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        try:
            d = parsedate_to_datetime(s)
        except (TypeError, ValueError):
            return None
    return d if d and d.tzinfo else (d.replace(tzinfo=timezone.utc) if d else None)


def _inc(provider, title, big, started, url, where="", au=False):
    return {"provider": provider, "title": (title or "").strip()[:160], "big": bool(big),
            "started": iso(started) if started else None, "url": url or "", "where": where[:120], "au": bool(au)}


def parse_statuspage(data, name, base):
    """Atlassian Statuspage /api/v2/incidents/unresolved.json. BIG = impact 'critical'."""
    out = []
    for i in (data or {}).get("incidents") or []:
        if i.get("status") in ("resolved", "postmortem"):
            continue
        where = ", ".join(c.get("name", "") for c in (i.get("components") or [])[:4])
        text = f"{i.get('name', '')} {where}"
        out.append(_inc(name, i.get("name"), i.get("impact") == "critical", _dt(i.get("created_at")),
                        i.get("shortlink") or base, where, bool(AU_REGIONS.search(text))))
    return out


def parse_gcp(data, now):
    """Google Cloud incidents.json (newest first). Active = no 'end'. BIG = severity 'high' and wide
    (3+ products or 3+ locations, or a global location)."""
    out = []
    for i in data or []:
        begin = _dt(i.get("begin"))
        if begin and now - begin > timedelta(days=7):
            break
        if i.get("end"):
            continue
        prods = [p.get("title", "") for p in i.get("affected_products") or []]
        locs = [l.get("title") or l.get("id") or "" for l in i.get("currently_affected_locations") or []]
        wide = len(prods) >= 3 or len(locs) >= 3 or any("global" in l.lower() or "multi-region" in l.lower() for l in locs)
        uri = i.get("uri") or ""
        out.append(_inc("Google Cloud", f"Google Cloud: {i.get('external_desc', '')}", i.get("severity") == "high" and wide,
                        begin, uri if uri.startswith("http") else f"https://status.cloud.google.com/{uri.lstrip('/')}",
                        ", ".join(locs[:4]), any(AU_REGIONS.search(l) for l in locs)))
    return out


AWS_PREFIX = re.compile(r"^\s*(Service disruption|Service impact|Informational message|Service is operating normally|"
                        r"Performance issues?)\s*:\s*", re.I)
RESOLVED = re.compile(r"\[RESOLVED\]|operating normally", re.I)


def parse_aws(raw, now, hours=6):
    """AWS public RSS: one item per update. An event is active if its newest update in the last few hours is not
    '[RESOLVED]'. BIG = 'Service disruption' / 'Service impact'."""
    root = ET.fromstring(raw)
    latest = {}
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        d = _dt(it.findtext("pubDate"))
        if not d or now - d > timedelta(hours=hours):
            continue
        guid = it.findtext("guid") or it.findtext("link") or title
        m = re.search(r"#([a-z0-9-]+?)_\d+", guid)  # e.g. ...#ec2-us-east-1_1700000000
        body = AWS_PREFIX.sub("", RESOLVED.sub("", title)).strip(" :")
        key = m.group(1) if m else body.lower()
        if key not in latest or d > latest[key][0]:
            latest[key] = (d, title, guid, m.group(1) if m else "")
    out = []
    for d, title, guid, where in latest.values():
        if RESOLVED.search(title):
            continue
        big = bool(re.match(r"\s*Service (disruption|impact)", title, re.I))
        out.append(_inc("AWS", f"AWS: {AWS_PREFIX.sub('', title)}", big, d, guid if guid.startswith("http") else
                        "https://health.aws.amazon.com/health/status", where, bool(AU_REGIONS.search(f"{where} {title}"))))
    return out


def parse_azure(raw, now, hours=6):
    """Azure status RSS lists only broad-impact incidents, so a recent unresolved one is BIG."""
    out = []
    root = ET.fromstring(raw)
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        d = _dt(it.findtext("pubDate"))
        if not title or not d or now - d > timedelta(hours=hours):
            continue
        done = bool(re.search(r"\b(mitigated|resolved)\b", title, re.I))
        desc = it.findtext("description") or ""
        out.append(_inc("Azure", f"Microsoft Azure: {title}", not done, d, it.findtext("link") or AZURE, "",
                        bool(AU_REGIONS.search(f"{title} {desc[:500]}"))))
    return out


def fetch_status():
    """Active incidents on the providers' own status pages. Each page fails on its own."""
    now = now_utc()
    plan = {
        "Google Cloud": lambda: parse_gcp(get_json(GCP, timeout=30), now),
        "AWS": lambda: parse_aws(http_get(AWS, timeout=30), now),
        "Azure": lambda: parse_azure(http_get(AZURE, timeout=30), now),
        **{p["name"]: (lambda p=p: parse_statuspage(get_json(p["url"].rstrip("/") + "/api/v2/incidents/unresolved.json",
                                                              timeout=20), p["name"], p["url"]))
           for p in STATUSPAGES},
    }
    active, ok, errors = [], [], []
    for name, fn in plan.items():
        try:
            active += fn()
            ok.append(name)
        except Exception as e:  # noqa: BLE001
            errors.append(f"{name}: {type(e).__name__}: {e}"[:160])
    if not ok:
        raise RuntimeError("; ".join(errors[:3]))
    active.sort(key=lambda i: (not i["big"], not i["au"], i["provider"]))
    return {"checked": ok, "active": active, "errors": errors}


# ---------- UKMTO (probe only, until its format is known) ----------

UKMTO_PAGES = CFG.get("ukmto_probe", ["https://www.ukmto.org/recent-incidents", "https://www.ukmto.org/"])
PROBE_DIR = DATA_ROOT / "data" / "probe"


def fetch_ukmto():
    """Fetch the UKMTO pages and keep a sample (first 200 KB, refreshed every 6 h) so a parser can be written.
    Also lists any feed or API links the HTML mentions. Never alerts."""
    pages, errors = [], []
    for i, url in enumerate(UKMTO_PAGES):
        if i:
            time.sleep(1)
        try:
            raw = http_get(url, timeout=25, accept="text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8")
        except Exception as e:  # noqa: BLE001
            errors.append(f"{url}: {type(e).__name__}: {e}"[:160])
            continue
        text = raw.decode("utf-8", "replace")
        hints = sorted(set(re.findall(r"""["'(]((?:https?://[^"' )]*ukmto[^"' )]*|/)[^"' )]*(?:api|rss|feed|json|incident)[^"' )]*)""",
                                      text, re.I)))[:30]
        slug = re.sub(r"\W+", "_", url.split("//", 1)[-1]).strip("_")[:60]
        path = PROBE_DIR / f"ukmto_{slug}.html"
        try:
            if not path.exists() or time.time() - path.stat().st_mtime > 6 * 3600:
                PROBE_DIR.mkdir(parents=True, exist_ok=True)
                path.write_text(text[:200_000])
        except OSError as e:
            errors.append(f"save {path.name}: {e}"[:160])
        js_hints = []
        if i == 0:
            jp = PROBE_DIR / "ukmto_js.json"
            try:
                if not jp.exists() or time.time() - jp.stat().st_mtime > 6 * 3600:
                    PROBE_DIR.mkdir(parents=True, exist_ok=True)
                    jp.write_text(json.dumps(_js_hints(text, errors), indent=1))
                js_hints = f"data/probe/{jp.name}"
            except OSError as e:
                errors.append(f"save {jp.name}: {e}"[:160])
        pages.append({"url": url, "bytes": len(raw), "has_next_data": "__NEXT_DATA__" in text, "js_hints": js_hints,
                      "incident_words": len(re.findall(r"\b(incident|attack|vessel|master reports)\b", text, re.I)),
                      "links": hints, "sample": f"data/probe/{path.name}"})
    if not pages:
        raise RuntimeError("; ".join(errors[:3]))
    return {"probe": True, "pages": pages, "errors": errors}


JS_URL = re.compile(r"""["'`]((?:https?:)?//[^"'`\s]{3,200}|/[A-Za-z0-9_./?=&{}$-]{2,200})["'`]""")


def _js_hints(html_text, errors, limit=10):
    """The incident list is loaded by the page's own scripts: read them (at most `limit`, 1 per second) and
    keep any URL-looking strings that mention an API, incidents or a feed, plus a little context."""
    srcs = [u if u.startswith("http") else "https://www.ukmto.org" + u
            for u in re.findall(r'src="([^"]+/_next/static/chunks/[^"]+\.js)"', html_text)]
    srcs.sort(key=lambda u: ("pages/" not in u and "_app" not in u, u))  # page code first
    hints = []
    for n, src in enumerate(srcs[:limit]):
        if n:
            time.sleep(1)
        try:
            js = http_get(src, timeout=25).decode("utf-8", "replace")
        except Exception as e:  # noqa: BLE001
            errors.append(f"{src[-50:]}: {type(e).__name__}"[:160])
            continue
        for m in JS_URL.finditer(js):
            u = m.group(1)
            if re.search(r"api|incident|feed|rss|graphql|sitecore|layout", u, re.I) and not u.endswith((".js", ".css", ".svg", ".png")):
                ctx = js[max(0, m.start() - 80):m.end() + 80].replace("\n", " ")
                hints.append({"file": src.rsplit("/", 1)[-1][:60], "url": u[:200], "context": ctx[:300]})
        for m in re.finditer(r"RecentIncident|recentIncident|incidents?\b", js):
            if len(hints) >= 60:
                break
            hints.append({"file": src.rsplit("/", 1)[-1][:60], "url": "", "context": js[max(0, m.start() - 150):m.end() + 250][:400]})
    seen, out = set(), []
    for h in hints:
        k = (h["url"], h["context"][:80])
        if k not in seen:
            seen.add(k)
            out.append(h)
    return out[:60]
