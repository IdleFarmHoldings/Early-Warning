"""Local South Australian hazards: CFS bushfire incidents and warnings, fire danger ratings and total fire bans,
Bureau of Meteorology warnings, and AEMO 'lack of reserve' (grid supply) notices for the SA region.
Feed layouts are not all documented, so parsing is defensive and a raw sample is kept for checking."""
import json
import re
import xml.etree.ElementTree as ET

from common import CONFIG, http_get

CFS_INCIDENTS = "https://data.eso.sa.gov.au/prod/cfs/criimson/cfs_current_incidents.json"
CFS_CAP = "https://data.eso.sa.gov.au/feeds/prod/cap-au.xml"
CFS_FDR = "https://data.eso.sa.gov.au/prod/cfs/criimson/fireDangerRating.xml"
BOM_SA = "http://www.bom.gov.au/fwo/IDZ00057.warnings_sa.xml"
NEMWEB_NOTICES = "https://nemweb.com.au/Reports/Current/Market_Notice/"

LEVELS = ["Emergency Warning", "Watch and Act", "Advice"]
RATINGS = ["Catastrophic", "Extreme", "High", "Moderate", "No Rating"]


def _local(tag):
    return tag.rsplit("}", 1)[-1].lower()


def _texts(el):
    return {_local(c.tag): (c.text or "").strip() for c in el.iter() if (c.text or "").strip()}


def _level(text):
    t = (text or "").lower()
    for lv in LEVELS:
        if lv.lower() in t:
            return lv
    return None


# ---------- CFS incidents (JSON) ----------
def parse_cfs_incidents(data):
    rows = data if isinstance(data, list) else next((v for v in (data or {}).values() if isinstance(v, list)), [])
    out = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        flat = " ".join(str(v) for v in r.values() if isinstance(v, (str, int, float)))
        kind = next((str(r[k]) for k in r if k.lower() in ("type", "incident_type", "incidenttype")), "")
        name = next((str(r[k]) for k in r if k.lower() in ("location_name", "location", "name", "title", "suburb")), "")
        status = next((str(r[k]) for k in r if k.lower() in ("status", "incident_status")), "")
        out.append({"type": kind, "where": name, "status": status, "level": _level(flat),
                    "fire": bool(re.search(r"fire|burn|smoke", kind + " " + flat, re.I))})
    return out


# ---------- CFS warnings (CAP-AU) ----------
def parse_cap(raw):
    root = ET.fromstring(raw)
    out = []
    for info in [e for e in root.iter() if _local(e.tag) == "info"]:
        t = _texts(info)
        headline = t.get("headline") or t.get("event") or ""
        level = _level(" ".join([headline, t.get("event", ""), t.get("description", "")[:300],
                                 " ".join(v for k, v in t.items() if k == "value")]))
        out.append({"headline": headline[:160], "event": t.get("event", ""), "severity": t.get("severity", ""),
                    "area": t.get("areadesc", "")[:120], "level": level})
    return out


# ---------- Fire danger ratings & total fire bans ----------
def parse_fdr(raw, districts):
    """Return {district: {"ratings": [...], "fire_ban": bool}} for the districts we care about."""
    root = ET.fromstring(raw)
    out = {}
    for el in root.iter():
        texts = [(c.text or "").strip() for c in el.iter() if (c.text or "").strip()]
        joined = " | ".join(texts)
        present = [d for d in districts if d.lower() in joined.lower()]
        if len(present) != 1:  # only elements describing a single district
            continue
        for d in present:
            if len(texts) < 40:
                ratings = [r for r in RATINGS for x in texts if x.lower() == r.lower() or x.lower().startswith(r.lower())]
                ban = bool(re.search(r"total fire ban|\bTFB\b|fire ban.*(yes|declared|true)", joined, re.I)) and \
                    not re.search(r"no total fire ban|fire ban.*(no|false)", joined, re.I)
                cur = out.get(d)
                if cur is None or len(ratings) > len(cur["ratings"]):  # keep the element with the most ratings
                    out[d] = {"ratings": ratings[:4], "fire_ban": ban, "text": joined[:200]}
    return out


def worst(ratings):
    for r in RATINGS:
        if r in ratings:
            return r
    return None


# ---------- Bureau of Meteorology warnings for SA ----------
def parse_bom(raw):
    root = ET.fromstring(raw)
    out = []
    for item in [e for e in root.iter() if _local(e.tag) == "item"]:
        t = _texts(item)
        title = t.get("title", "")
        if title:
            out.append({"title": title[:160], "link": t.get("link", ""),
                        "severe": bool(re.search(r"severe|heatwave|extreme heat|flood warning|damaging winds|fire weather", title, re.I))})
    return out


# ---------- AEMO lack-of-reserve notices ----------
LOR_RE = re.compile(r"(Actual|Forecast)\s+(?:Lack Of Reserve|LOR)\s*(?:Level\s*)?(\d)?.*?(?:\(LOR(\d)\))?.*?in the\s+(\w+)\s+region", re.I | re.S)


def parse_notice(text):
    """One AEMO market notice -> {"kind", "level", "region", "cancelled"} or None if not a reserve notice."""
    if not re.search(r"lack of reserve|\bLOR[123]\b", text, re.I):
        return None
    m = LOR_RE.search(text)
    if not m:
        return None
    level = m.group(2) or m.group(3)
    region = m.group(4).upper()
    region = {"SOUTH": "SA", "SA1": "SA"}.get(region, region)
    return {"kind": m.group(1).title(), "level": int(level) if level else None, "region": region,
            "cancelled": bool(re.search(r"cancellation|cancelled", text[:600], re.I))}


def fetch_aemo(state, max_new=40):
    listing = http_get(NEMWEB_NOTICES, timeout=30).decode("utf-8", "replace")
    files = sorted(set(re.findall(r'href="([^"]*MKTNOTICE[^"]*)"', listing, re.I)))
    seen = state.setdefault("aemo_seen", [])
    new = [f for f in files if f not in seen][-max_new:]
    found = state.setdefault("aemo_lor", [])
    for f in new:
        url = f if f.startswith("http") else "https://nemweb.com.au" + (f if f.startswith("/") else "/Reports/Current/Market_Notice/" + f)
        try:
            n = parse_notice(http_get(url, timeout=20).decode("utf-8", "replace"))
        except Exception:  # noqa: BLE001
            continue
        if n:
            n["file"] = f.rsplit("/", 1)[-1]
            found.append(n)
    state["aemo_seen"] = (seen + new)[-400:]
    state["aemo_lor"] = found[-60:]
    sa = [n for n in found[-60:] if n["region"] == "SA"]
    active = [n for n in sa if not n["cancelled"]][-10:]
    return {"sa_notices": active, "sa_max_level": max((n["level"] or 0 for n in active), default=0),
            "sa_actual": any(n["kind"] == "Actual" for n in active), "checked_files": len(new)}


def fetch_local(state):
    lc = CONFIG["local"]
    out, errors, samples = {}, [], {}

    def part(name, fn, sample_url=None):
        try:
            out[name] = fn()
        except Exception as e:  # noqa: BLE001
            errors.append(f"{name}: {type(e).__name__}: {e}"[:200])

    def incidents():
        raw = http_get(CFS_INCIDENTS, timeout=30)
        samples["cfs_incidents"] = raw[:1500].decode("utf-8", "replace")
        return parse_cfs_incidents(json.loads(raw))

    def cap():
        raw = http_get(CFS_CAP, timeout=30)
        samples["cfs_cap"] = raw[:1500].decode("utf-8", "replace")
        return parse_cap(raw)

    def fdr():
        raw = http_get(CFS_FDR, timeout=30)
        samples["cfs_fdr"] = raw[:2500].decode("utf-8", "replace")
        return parse_fdr(raw, lc["fire_districts"])

    def bom():
        raw = http_get(BOM_SA, timeout=30)
        samples["bom"] = raw[:1000].decode("utf-8", "replace")
        return parse_bom(raw)

    part("incidents", incidents)
    part("warnings", cap)
    part("fire_danger", fdr)
    part("bom", bom)
    part("grid", lambda: fetch_aemo(state))
    if not out:
        raise RuntimeError("; ".join(errors[:3]))
    inc = out.get("incidents") or []
    warn = out.get("warnings") or []
    fd = out.get("fire_danger") or {}
    out["summary"] = {
        "fires_active": sum(1 for i in inc if i["fire"]),
        "emergency_warnings": [w["headline"] for w in warn if w["level"] == "Emergency Warning"]
        + [f"{i['type']} {i['where']}" for i in inc if i["level"] == "Emergency Warning"],
        "watch_and_act": [w["headline"] for w in warn if w["level"] == "Watch and Act"]
        + [f"{i['type']} {i['where']}" for i in inc if i["level"] == "Watch and Act"],
        "worst_fire_danger": {d: worst(v["ratings"]) for d, v in fd.items()},
        "fire_ban_districts": [d for d, v in fd.items() if v["fire_ban"]],
        "bom_severe": [b["title"] for b in (out.get("bom") or []) if b["severe"]],
        "grid_lor_level": (out.get("grid") or {}).get("sa_max_level", 0),
        "grid_lor_actual": (out.get("grid") or {}).get("sa_actual", False),
    }
    out["errors"] = errors
    out["samples"] = samples
    return out
