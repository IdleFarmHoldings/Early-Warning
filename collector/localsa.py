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
BOM_SA = "ftp://ftp.bom.gov.au/anon/gen/fwo/IDZ00057.warnings_sa.xml"  # BoM's public FTP is meant for automated access
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
    """Return {district: {"ratings": [today, tomorrow, ...], "fire_ban": bool}} for the districts we care about.
    CFS layout: <area type="fire-district" description="Adelaide Metropolitan"><forecast-period index="1">
    <text type="fire_danger">High</text><text type="fire_ban">false</text>..."""
    root = ET.fromstring(raw)
    out = {}
    for area in [e for e in root.iter() if _local(e.tag) == "area" and e.get("type") == "fire-district"]:
        name = area.get("description", "")
        if not any(name.lower() == d.lower() for d in districts):
            continue
        periods = sorted([p for p in area if _local(p.tag) == "forecast-period"], key=lambda p: int(p.get("index", "99")))
        ratings, bans = [], []
        for p in periods[:2]:  # today and tomorrow
            vals = {t.get("type"): (t.text or "").strip() for t in p if _local(t.tag) == "text"}
            if vals.get("fire_danger"):
                ratings.append(vals["fire_danger"])
            bans.append(vals.get("fire_ban", "").lower() == "true")
        out[name] = {"ratings": ratings, "fire_ban": any(bans), "fire_ban_today": bool(bans and bans[0]),
                     "text": " / ".join(ratings)}
    if out:
        return out
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


def parse_bom_cap(raw):
    """Bureau CAP warning -> [{"title", "severe", "expires"}] (expires as UTC ISO string, or "")."""
    from datetime import datetime, timezone
    root = ET.fromstring(raw)
    out = []
    for info in [e for e in root.iter() if _local(e.tag) == "info"]:
        t = _texts(info)
        title = t.get("headline") or t.get("event") or ""
        exp = t.get("expires", "")
        try:
            exp = datetime.fromisoformat(exp).astimezone(timezone.utc).isoformat() if exp else ""
        except ValueError:
            exp = ""
        if title:
            out.append({"title": title[:160], "expires": exp, "link": "",
                        "severe": bool(re.search(r"severe|heatwave|extreme heat|flood warning|damaging winds|fire weather|destructive", title, re.I))})
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


# ---------- Traffic SA road closures and incidents (ArcGIS) ----------
ROAD_LAYERS = {
    "closures": ["https://maps.sa.gov.au/arcgis/rest/services/DPTIExtTransport/TrafficSAOpenData/MapServer/1",
                 "https://dit-cms-prod.geohub.sa.gov.au/arcgis/rest/services/TrafficSATime_OpenData/TrafficSATime_OpenData/MapServer/2"],
    "incidents": ["https://maps.sa.gov.au/arcgis/rest/services/DPTIExtTransport/TrafficSAOpenData/MapServer/2",
                  "https://dit-cms-prod.geohub.sa.gov.au/arcgis/rest/services/TrafficSATime_OpenData/TrafficSATime_OpenData/MapServer/0"],
}


def _arcgis_rows(url):
    data = json.loads(http_get(url + "/query?where=1%3D1&outFields=*&returnGeometry=false&f=json", timeout=30))
    if data.get("error"):
        raise RuntimeError(str(data["error"])[:150])
    return [f.get("attributes", {}) for f in data.get("features", [])]


def summarise_roads(rows):
    out = []
    for r in rows:
        if "LOCAL_ROAD_NAME" in r or "PLOT_DETAILS" in r:
            det = (r.get("PLOT_DETAILS") or "").replace("\r", " ").replace("\n", " ")
            det = re.sub(r"\s+", " ", re.sub(r"Ref#\s*\d+", "", det)).strip()
            out.append({"road": (r.get("LOCAL_ROAD_NAME") or "").title()[:80],
                        "suburb": (r.get("START_SUBURB") or "").title()[:40], "type": det[:120],
                        "hazard": bool(re.search(r"fire|flood|storm|tree|power|emergency|hazard|smoke|washout", det, re.I))})
            continue
        txt = " ".join(str(v) for v in r.values() if isinstance(v, str))
        road = next((str(r[k]) for k in r if re.search(r"road|street|location|name", k, re.I) and isinstance(r[k], str)), "")
        kind = next((str(r[k]) for k in r if re.search(r"type|category|description", k, re.I) and isinstance(r[k], str)), "")
        out.append({"road": road[:80], "type": kind[:80],
                    "hazard": bool(re.search(r"fire|flood|storm|tree|power|emergency|hazard|smoke", txt, re.I))})
    return out


def fetch_roads(samples):
    res = {}
    for name, urls in ROAD_LAYERS.items():
        for u in urls:
            try:
                rows = _arcgis_rows(u)
                samples[f"roads_{name}"] = json.dumps(rows[:2])[:800]
                res[name] = summarise_roads(rows)
                break
            except Exception as e:  # noqa: BLE001
                samples[f"roads_{name}_err"] = f"{u}: {type(e).__name__}: {e}"[:200]
    if not res:
        raise RuntimeError("no road layers reachable")
    return res


# ---------- SA Health emergency departments ----------
ED_SHELL = "https://www.sahealth.sa.gov.au/wps/themes/html/Portal/js/OBI_DATA/EDDashboard_rwd.html"
ED_DATA = "https://www.sahealth.sa.gov.au/wps/themes/html/Portal/js/OBI_DATA/json/ED001.json"
ED_NAMES = {"FMC": "Flinders Medical Centre", "LMH": "Lyell McEwin", "RAH": "Royal Adelaide", "TQEH": "Queen Elizabeth",
            "MOD": "Modbury", "NHS": "Noarlunga", "NOA": "Noarlunga", "WCH": "Women's and Children's"}
ED_VERY_BUSY = {"cap_pct": 100, "wait_min": 240}


def parse_ed_rows(rows):
    """SA Health ED001.json rows: HOSP_SHORT, CAP (% of ED capacity in use), AVG_WAIT (minutes), WTBS (waiting to be seen)."""
    out = []
    for r in rows:
        try:
            cap, wait = float(r.get("CAP") or 0), float(r.get("AVG_WAIT") or 0)
            wtbs = int(float(r.get("WTBS") or 0))
        except ValueError:
            continue
        code = (r.get("HOSP_SHORT") or "").strip()
        very = cap >= ED_VERY_BUSY["cap_pct"] or wait >= ED_VERY_BUSY["wait_min"]
        out.append({"hospital": ED_NAMES.get(code, code), "code": code, "capacity_pct": cap, "avg_wait_min": round(wait),
                    "waiting": wtbs, "status": "Very busy" if very else ("Busy" if cap >= 85 or wait >= 120 else "Normal"),
                    "as_of": r.get("DTM", "")})
    return out


def _abs(base, ref):
    from urllib.parse import urljoin
    return urljoin(base, ref)


def discover_ed(samples):
    """SA Health ED data: the known data file first, then discovery from the dashboard page as a fallback."""
    try:
        rows = json.loads(http_get(ED_DATA, timeout=30))
        recs = parse_ed_rows(rows if isinstance(rows, list) else [])
        if recs:
            return recs
    except Exception as e:  # noqa: BLE001
        samples["ed_direct_err"] = f"{type(e).__name__}: {e}"[:200]
    return _discover_ed(samples)


def _discover_ed(samples):
    """The dashboard loads its data with JavaScript; find and fetch the data file it uses."""
    html = http_get(ED_SHELL, timeout=30).decode("utf-8", "replace")
    refs = re.findall(r"""(?:src|href)=["']([^"']+\.(?:js|json|csv|xml)[^"']*)["']""", html, re.I)
    refs += re.findall(r"""["']([^"'\s]+\.(?:json|csv|xml)(?:\?[^"']*)?)["']""", html, re.I)
    found = []
    for r in refs[:12]:
        u = _abs(ED_SHELL, r)
        if any(x in u for x in ("jquery", "bootstrap", "analytics", "google")):
            continue
        try:
            body = http_get(u, timeout=20).decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            continue
        if u.endswith(".js") or ".js?" in u:
            for d in re.findall(r"""["']([^"'\s]+\.(?:json|csv|xml)(?:\?[^"']*)?)["']""", body, re.I)[:8]:
                found.append(_abs(u, d))
        else:
            found.append(u)
    samples["ed_candidates"] = " ".join(dict.fromkeys(found))[:1200]
    for u in dict.fromkeys(found):
        try:
            body = http_get(u, timeout=20).decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            continue
        samples["ed_data_url"] = u
        samples["ed_data"] = body[:2000]
        try:
            return parse_ed_rows(json.loads(body)) or parse_ed(body)
        except ValueError:
            return parse_ed(body)
    raise RuntimeError("ED data file not found yet (see debug sample)")


def parse_ed(body):
    """Hospitals with their busy status. Accepts JSON or XML; keeps any record that names a hospital and a status."""
    recs = []
    try:
        data = json.loads(body)
        stack = [data]
        while stack:
            x = stack.pop()
            if isinstance(x, dict):
                name = next((str(v) for k, v in x.items() if re.search(r"hosp|site|name", k, re.I) and isinstance(v, str)), None)
                status = next((str(v) for k, v in x.items() if re.search(r"status|capacity|state", k, re.I) and isinstance(v, str)), None)
                if name and status:
                    recs.append({"hospital": name[:60], "status": status[:30]})
                stack.extend(x.values())
            elif isinstance(x, list):
                stack.extend(x)
    except ValueError:
        root = ET.fromstring(body)
        for el in root.iter():
            t = {_local(c.tag): (c.text or "").strip() for c in el}
            name = next((v for k, v in t.items() if re.search(r"hosp|site|name", k) and v), None)
            status = next((v for k, v in t.items() if re.search(r"status|capacity", k) and v), None)
            if name and status:
                recs.append({"hospital": name[:60], "status": status[:30]})
    return recs[:40]


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
        txt = raw.decode("utf-8", "replace")
        i = txt.find("Adelaide Metropolitan")
        samples["cfs_fdr"] = txt[max(0, i - 300):i + 1500] if i >= 0 else txt[:1500]
        return parse_fdr(raw, lc["fire_districts"])

    def bom():
        """Active Bureau warnings for SA: the CAP files (IDS*.cap.xml) on BoM's public FTP, skipping expired ones."""
        import ftplib
        import io
        from datetime import datetime, timezone
        out = []
        with ftplib.FTP("ftp.bom.gov.au", timeout=30) as f:
            f.login()
            f.cwd("/anon/gen/fwo")
            names = sorted(n for n in f.nlst() if re.match(r"^IDS\d+\.cap\.xml$", n))[:20]
            samples["bom_files"] = " ".join(names)
            for n in names:
                buf = io.BytesIO()
                f.retrbinary(f"RETR {n}", buf.write)
                for w in parse_bom_cap(buf.getvalue()):
                    if not w["expires"] or w["expires"] > datetime.now(timezone.utc).isoformat():
                        out.append(w)
        return out

    part("incidents", incidents)
    part("warnings", cap)
    part("fire_danger", fdr)
    part("bom", bom)
    part("grid", lambda: fetch_aemo(state))
    part("roads", lambda: fetch_roads(samples))
    part("ed", lambda: discover_ed(samples))
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
        "road_closures": len((out.get("roads") or {}).get("closures") or []),
        "road_incidents": len((out.get("roads") or {}).get("incidents") or []),
        "road_hazards": [f"{r['road']}{', ' + r.get('suburb') if r.get('suburb') else ''}: {r['type']}".strip() for k in ("closures", "incidents")
                         for r in ((out.get("roads") or {}).get(k) or []) if r["hazard"]][:5],
        "ed_very_busy": [e["hospital"] for e in (out.get("ed") or []) if re.search(r"very busy", e["status"], re.I)],
        "ed": [{k: e.get(k) for k in ("hospital", "status", "capacity_pct", "avg_wait_min", "waiting")} for e in (out.get("ed") or [])][:12],
        "ed_known": len(out.get("ed") or []),
    }
    out["errors"] = errors
    out["samples"] = samples
    return out
