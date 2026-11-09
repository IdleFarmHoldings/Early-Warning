"""Cyber threat picture from free, keyless sources. Each part fails independently.

Vectors watched:
- Exploited vulnerabilities (CISA KEV): spikes in newly exploited flaws, especially edge devices (VPNs, firewalls)
  that state actors use for initial access and pre-positioning.
- Ransomware (ransomware.live leak-site tracker): victims by country, Australian victims vs normal.
- Infrastructure probing (SANS ISC / DShield): scanning of industrial-control (OT) and remote-access ports vs normal.
- Attack sources (DShield top attacking IPs, geolocated) and botnet command servers (abuse.ch Feodo Tracker).
- Global threat level (SANS ISC Infocon).
"""
import json
import urllib.request
from datetime import datetime, timedelta, timezone

from common import CONFIG, UA, get_json, http_get, iso

KEV = "https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json"
INFOCON = "https://isc.sans.edu/api/infocon?json"
PORTHIST = "https://isc.sans.edu/api/porthistory/{port}/{start}/{end}?json"
ISC_SOURCES = "https://isc.sans.edu/api/sources/attacks/100?json"
FEODO = "https://feodotracker.abuse.ch/downloads/ipblocklist.json"
RANSOM_RECENT = ["https://api.ransomware.live/v2/recentvictims", "https://api.ransomware.live/recentvictims"]
RANSOM_COUNTRY = "https://api.ransomware.live/v2/countryvictims/{cc}"
IPAPI_BATCH = "http://ip-api.com/batch?fields=status,countryCode,query"


def _date(s):
    s = (s or "").strip().replace("Z", "")
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s[:26], fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _median(xs):
    xs = sorted(xs)
    return xs[len(xs) // 2] if xs else None


def weekly_spike(dates, now, weeks=12):
    """Count in the last 7 days vs the median of the previous `weeks` weeks."""
    last = sum(1 for d in dates if d and now - d <= timedelta(days=7))
    prior = []
    for w in range(1, weeks + 1):
        lo, hi = now - timedelta(days=7 * (w + 1)), now - timedelta(days=7 * w)
        prior.append(sum(1 for d in dates if d and lo < d <= hi))
    med = _median(prior)
    return {"last_7d": last, "usual_7d": med, "ratio": round(last / med, 2) if med else None}


# ---------- CISA Known Exploited Vulnerabilities ----------
def parse_kev(data, now):
    cc = CONFIG["cyber"]
    vulns = data.get("vulnerabilities") or []
    dates = [_date(v.get("dateAdded")) for v in vulns]
    edge = [_date(v.get("dateAdded")) for v in vulns if (v.get("vendorProject") or "").lower() in cc["edge_vendors"]]
    recent = [v for v, d in zip(vulns, dates) if d and now - d <= timedelta(days=7)]
    return {
        "all": weekly_spike(dates, now), "edge": weekly_spike(edge, now),
        "ransomware_linked_7d": sum(1 for v in recent if (v.get("knownRansomwareCampaignUse") or "").lower() == "known"),
        "recent": [{"cve": v.get("cveID"), "vendor": v.get("vendorProject"), "product": v.get("product"),
                    "name": v.get("vulnerabilityName"), "added": v.get("dateAdded"),
                    "edge": (v.get("vendorProject") or "").lower() in cc["edge_vendors"],
                    "ransomware": (v.get("knownRansomwareCampaignUse") or "").lower() == "known"} for v in recent][:20],
    }


# ---------- Ransomware leak sites ----------
def _victim(v):
    return {"name": v.get("victim") or v.get("post_title") or v.get("name") or "",
            "group": v.get("group") or v.get("group_name") or "",
            "country": (v.get("country") or "").upper()[:2],
            "sector": v.get("activity") or v.get("sector") or "",
            "date": v.get("attackdate") or v.get("discovered") or v.get("published") or ""}


def parse_ransom(recent, au_hist, now):
    victims = [_victim(v) for v in (recent if isinstance(recent, list) else recent.get("victims", []))]
    week = [v for v in victims if (_date(v["date"]) or now) >= now - timedelta(days=7)]
    by_country = {}
    for v in week:
        if v["country"]:
            by_country[v["country"]] = by_country.get(v["country"], 0) + 1
    au = [_victim(v) for v in (au_hist if isinstance(au_hist, list) else (au_hist or {}).get("victims", []))]
    au_spike = weekly_spike([_date(v["date"]) for v in au], now) if au else None
    return {"victims_7d": len(week), "by_country_7d": by_country, "au": au_spike,
            "au_recent": [v for v in au if (_date(v["date"]) or now - timedelta(days=99)) >= now - timedelta(days=14)][:10]}


# ---------- SANS ISC ----------
def _rows(data):
    """ISC wraps lists in different shapes: a list, {"porthistory": [...]}, or {"0": {...}, "1": {...}}."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for v in data.values():
            if isinstance(v, list):
                return v
        vals = [v for v in data.values() if isinstance(v, dict)]
        if vals:
            return vals
    return []


def parse_porthistory(rows):
    """Distinct scanning sources on the latest full day vs the median of the earlier days."""
    vals = []
    for r in _rows(rows):
        if isinstance(r, dict) and (r.get("date") or r.get("day")):
            try:
                vals.append((r.get("date") or r.get("day"), int(r.get("sources") or 0), int(r.get("records") or 0)))
            except (TypeError, ValueError):
                continue
    vals.sort()
    if len(vals) < 6:
        return None
    *prev, last = vals[:-1] if len(vals) > 7 else vals  # the newest day is often partial
    med = _median([v[1] for v in prev])
    return {"date": last[0], "sources": last[1], "usual": med, "ratio": round(last[1] / med, 2) if med else None}


def _ip(s):
    try:
        return ".".join(str(int(p)) for p in s.split("."))
    except (AttributeError, ValueError):
        return None


def geolocate(ips):
    """Country codes for up to 100 IPs (ip-api.com batch, free tier)."""
    body = json.dumps([{"query": i} for i in ips[:100]]).encode()
    req = urllib.request.Request(IPAPI_BATCH, data=body, headers={"User-Agent": UA, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return {x["query"]: x.get("countryCode") for x in json.loads(r.read()) if x.get("status") == "success"}


def fetch_cyber():
    cc = CONFIG["cyber"]
    now = datetime.now(timezone.utc)
    out, errors = {}, []

    def part(name, fn):
        try:
            out[name] = fn()
        except Exception as e:  # noqa: BLE001
            errors.append(f"{name}: {type(e).__name__}: {e}"[:160])

    part("kev", lambda: parse_kev(get_json(KEV, timeout=40), now))
    part("infocon", lambda: (get_json(INFOCON).get("status") or "").lower())

    def ports():
        start, end = (now - timedelta(days=15)).strftime("%Y-%m-%d"), now.strftime("%Y-%m-%d")
        res = {}
        for p in cc["ports"]:
            try:
                raw = get_json(PORTHIST.format(port=p["port"], start=start, end=end))
                parsed = parse_porthistory(raw)
                if parsed is None:
                    errors.append(f"port {p['port']} unparsed: {json.dumps(raw)[:200]}")
                res[str(p["port"])] = {"name": p["name"], "group": p["group"], **(parsed or {})}
            except Exception as e:  # noqa: BLE001
                errors.append(f"port {p['port']}: {type(e).__name__}"[:80])
        return res
    part("ports", ports)

    def sources():
        rows = get_json(ISC_SOURCES)
        rows = rows if isinstance(rows, list) else []
        ips = [(_ip(r.get("ip")), int(r.get("attacks") or r.get("count") or 0)) for r in rows if isinstance(r, dict)]
        ips = [(i, n) for i, n in ips if i]
        geo = geolocate([i for i, _ in ips])
        by = {}
        for i, n in ips:
            c = geo.get(i)
            if c:
                by[c] = by.get(c, 0) + n
        return {"top_ips": len(ips), "by_country": by}
    part("sources", sources)

    def feodo():
        rows = json.loads(http_get(FEODO, timeout=30))
        rows = rows if isinstance(rows, list) else []
        by, online, new7 = {}, 0, 0
        for r in rows:
            c = (r.get("country") or "").upper()
            if c:
                by[c] = by.get(c, 0) + 1
            if (r.get("status") or "").lower() == "online":
                online += 1
            d = _date(r.get("first_seen"))
            if d and now - d <= timedelta(days=7):
                new7 += 1
        return {"total": len(rows), "online": online, "new_7d": new7, "by_country": by}
    part("c2", feodo)

    def ransom():
        recent, last_err = None, None
        for u in RANSOM_RECENT:
            try:
                recent = get_json(u, timeout=30)
                break
            except Exception as e:  # noqa: BLE001
                last_err = e
        if recent is None:
            raise last_err
        try:
            au = get_json(RANSOM_COUNTRY.format(cc="AU"), timeout=30)
        except Exception as e:  # noqa: BLE001
            errors.append(f"ransom AU: {type(e).__name__}"[:80])
            au = None
        return parse_ransom(recent, au, now)
    part("ransomware", ransom)

    if not out:
        raise RuntimeError("; ".join(errors[:3]))
    ports_ = out.get("ports") or {}
    ics = [v["ratio"] for v in ports_.values() if v.get("group") == "ics" and v.get("ratio")]
    out["summary"] = {
        "infocon": out.get("infocon"),
        "kev_7d": (out.get("kev") or {}).get("all", {}).get("last_7d"),
        "kev_ratio": (out.get("kev") or {}).get("all", {}).get("ratio"),
        "kev_edge_7d": (out.get("kev") or {}).get("edge", {}).get("last_7d"),
        "ics_scan_ratio": max(ics) if ics else None,
        "ransom_au_7d": ((out.get("ransomware") or {}).get("au") or {}).get("last_7d"),
        "ransom_au_ratio": ((out.get("ransomware") or {}).get("au") or {}).get("ratio"),
        "c2_online": (out.get("c2") or {}).get("online"),
    }
    out["generated_at"] = iso(now)
    out["errors"] = errors
    return out
