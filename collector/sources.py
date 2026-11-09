"""Fetch and parse each data source. Parsers take raw bytes/JSON so they can be tested offline."""
import re
import time
import urllib.parse
import xml.etree.ElementTree as ET

from common import CONFIG, get_json, http_get

NS = {
    "gdacs": "http://www.gdacs.org",
    "geo": "http://www.w3.org/2003/01/geo/wgs84_pos#",
    "atom": "http://www.w3.org/2005/Atom",
    "dc": "http://purl.org/dc/elements/1.1/",
}


def _text(el, path, ns=NS):
    found = el.find(path, ns)
    return (found.text or "").strip() if found is not None and found.text else ""


def _float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ---------- US State Department travel advisories ----------
US_ADVISORIES = "https://travel.state.gov/_res/rss/TAsTWs.xml"
LEVEL_RE = re.compile(r"^(.*?)\s*-\s*Level\s*(\d)", re.I)


def parse_us_advisories(raw):
    out = {}
    root = ET.fromstring(raw)
    for item in root.iter("item"):
        title = _text(item, "title")
        m = LEVEL_RE.match(title)
        if not m:
            continue
        desc = _text(item, "description").lower()
        country = m.group(1).strip()
        out[country] = {
            "level": int(m.group(2)),
            "date": _text(item, "pubDate"),
            "link": _text(item, "link"),
            "departure": "ordered departure" in desc or "authorized departure" in desc,
        }
    return out


# ---------- UK FCDO travel advice updates ----------
UK_ADVICE = "https://www.gov.uk/foreign-travel-advice.atom"


def parse_uk_advice(raw):
    out = []
    root = ET.fromstring(raw)
    for e in root.findall("atom:entry", NS):
        link = e.find("atom:link", NS)
        out.append({
            "id": _text(e, "atom:id"),
            "title": _text(e, "atom:title"),
            "summary": _text(e, "atom:summary"),
            "updated": _text(e, "atom:updated"),
            "link": link.get("href") if link is not None else "",
        })
    return out


# ---------- GDACS disasters ----------
GDACS = "https://www.gdacs.org/xml/rss.xml"


def parse_gdacs(raw):
    out = []
    root = ET.fromstring(raw)
    for item in root.iter("item"):
        out.append({
            "id": _text(item, "gdacs:eventtype") + _text(item, "gdacs:eventid"),
            "type": _text(item, "gdacs:eventtype"),
            "alert": _text(item, "gdacs:alertlevel"),
            "country": _text(item, "gdacs:country"),
            "iso3": _text(item, "gdacs:iso3"),
            "title": _text(item, "title"),
            "current": _text(item, "gdacs:iscurrent").lower() == "true",
            "lat": _float(_text(item, "geo:Point/geo:lat")),
            "lon": _float(_text(item, "geo:Point/geo:long")),
            "link": _text(item, "link"),
        })
    return out


# ---------- USGS earthquakes ----------
USGS = "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/4.5_day.geojson"


def parse_usgs(data):
    out = []
    for f in data.get("features", []):
        p, g = f.get("properties", {}), f.get("geometry", {}).get("coordinates", [None, None])
        out.append({"id": f.get("id"), "mag": p.get("mag"), "place": p.get("place"), "tsunami": p.get("tsunami") == 1,
                    "time": p.get("time"), "lon": g[0], "lat": g[1], "url": p.get("url")})
    return out


# ---------- WHO Disease Outbreak News ----------
WHO = ("https://www.who.int/api/news/diseaseoutbreaknews?"
       + urllib.parse.urlencode({"$orderby": "PublicationDate desc", "$top": "20"}))
WHO_FALLBACK = "https://www.who.int/api/news/diseaseoutbreaknews"


def parse_who(data):
    items = data.get("value", data if isinstance(data, list) else [])
    out = []
    for it in items:
        title = it.get("OverrideTitle") or it.get("Title") or ""
        url = it.get("ItemDefaultUrl") or it.get("UrlName") or ""
        if url and not url.startswith("http"):
            url = "https://www.who.int/emergencies/disease-outbreak-news/item/" + url.strip("/").split("/")[-1]
        out.append({"id": it.get("DonId") or it.get("Id") or url or title, "title": title,
                    "date": it.get("PublicationDate", ""), "summary": re.sub("<[^>]+>", " ", it.get("Summary") or "")[:600],
                    "url": url})
    out.sort(key=lambda x: x["date"], reverse=True)
    return out[:20]


# ---------- Cyber: ACSC alerts and CISA advisories ----------
ACSC = "https://www.cyber.gov.au/rss/alerts"
CISA = "https://www.cisa.gov/cybersecurity-advisories/all.xml"


def parse_rss(raw, limit=20):
    out = []
    root = ET.fromstring(raw)
    for item in list(root.iter("item"))[:limit]:
        out.append({"id": _text(item, "guid") or _text(item, "link"), "title": _text(item, "title"),
                    "date": _text(item, "pubDate"), "link": _text(item, "link")})
    return out


# ---------- Markets (Yahoo Finance chart endpoint, unofficial) ----------
def market_quote(symbol):
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(symbol)}?range=5d&interval=1d"
    return parse_market(get_json(url))


def parse_market(data):
    res = data["chart"]["result"][0]
    closes = [c for c in res["indicators"]["quote"][0]["close"] if c is not None]
    meta = res.get("meta", {})
    price = meta.get("regularMarketPrice") or closes[-1]
    prev = meta.get("previousClose") or (closes[-2] if len(closes) >= 2 else None)
    change = (price - prev) / prev * 100 if prev else None
    return {"price": round(price, 4), "prev_close": round(prev, 4) if prev else None,
            "change_pct": round(change, 2) if change is not None else None}


# ---------- Aircraft: adsb.fi open data (1 request per second) ----------
ADSB = "https://opendata.adsb.fi/api"


def _adsb(path):
    time.sleep(1.1)
    return get_json(f"{ADSB}{path}")


def summarise_aircraft(acs, types):
    airborne, ground = {}, {}
    for a in acs:
        t = (a.get("t") or "").upper()
        if t not in types:
            continue
        rec = {"hex": a.get("hex"), "type": t, "reg": a.get("r"), "flight": (a.get("flight") or "").strip()}
        (ground if a.get("alt_baro") == "ground" else airborne)[a.get("hex")] = rec
    return airborne, ground


def fetch_aircraft():
    jc = CONFIG["jets"]
    lr = set(jc["long_range_types"])
    hub_air, by_hub, errors = {}, {}, []
    for h in jc["hubs"]:
        try:
            d = _adsb(f"/v3/lat/{h['lat']}/lon/{h['lon']}/dist/{jc['radius_nm']}")
            air, _ = summarise_aircraft(d.get("ac") or d.get("aircraft") or [], lr)
            hub_air.update(air)
            by_hub[h["name"]] = len(air)
        except Exception as e:  # noqa: BLE001 - one hub failing should not stop the rest
            errors.append(f"{h['name']}: {e}")
    retreat_total, by_retreat = {}, {}
    for r in jc["retreats"]:
        try:
            d = _adsb(f"/v3/lat/{r['lat']}/lon/{r['lon']}/dist/150")
            air, gnd = summarise_aircraft(d.get("ac") or d.get("aircraft") or [], lr)
            both = {**air, **gnd}
            retreat_total.update(both)
            by_retreat[r["name"]] = len(both)
        except Exception as e:  # noqa: BLE001
            errors.append(f"{r['name']}: {e}")
    mil = {"mil_total": None, "tankers": None, "nc3": None, "isr": None, "nc3_seen": []}
    try:
        acs = _adsb("/v2/mil").get("ac") or []
        airborne = [a for a in acs if a.get("alt_baro") != "ground"]
        def count(types):
            return sum(1 for a in airborne if (a.get("t") or "").upper() in set(types))
        mil.update(mil_total=len(airborne), tankers=count(jc["tanker_types"]), nc3=count(jc["nc3_types"]),
                   isr=count(jc["isr_types"]),
                   nc3_seen=[{"type": a.get("t"), "flight": (a.get("flight") or "").strip(), "hex": a.get("hex")}
                             for a in airborne if (a.get("t") or "").upper() in set(jc["nc3_types"])])
    except Exception as e:  # noqa: BLE001
        errors.append(f"military: {e}")
    if not by_hub and not by_retreat and mil["mil_total"] is None:
        raise RuntimeError("adsb.fi unreachable: " + "; ".join(errors[:2]))
    hubs_ok = len(by_hub) >= len(jc["hubs"]) * 0.75
    return {
        "bizjets_airborne": len(hub_air) if hubs_ok else None,
        "bizjets_by_hub": by_hub,
        "retreat_bizjets": len(retreat_total) if len(by_retreat) == len(jc["retreats"]) else None,
        "retreat_by_place": by_retreat,
        **mil,
        "errors": errors,
    }


def fetch_all():
    """Returns (results, health). Each source fails independently."""
    results, health = {}, {}
    plan = {
        "us_advisories": lambda: parse_us_advisories(http_get(US_ADVISORIES)),
        "uk_advice": lambda: parse_uk_advice(http_get(UK_ADVICE)),
        "gdacs": lambda: parse_gdacs(http_get(GDACS)),
        "usgs": lambda: parse_usgs(get_json(USGS)),
        "who": _fetch_who,
        "acsc": lambda: parse_rss(http_get(ACSC)),
        "cisa": lambda: parse_rss(http_get(CISA), limit=15),
        "markets": _fetch_markets,
        "aircraft": fetch_aircraft,
    }
    for name, fn in plan.items():
        t0 = time.time()
        try:
            results[name] = fn()
            health[name] = {"ok": True, "secs": round(time.time() - t0, 1)}
            errs = results[name].get("errors") if isinstance(results[name], dict) else None
            if errs:
                health[name]["partial_errors"] = errs[:5]
        except Exception as e:  # noqa: BLE001
            results[name] = None
            health[name] = {"ok": False, "error": f"{type(e).__name__}: {e}"[:300]}
    return results, health


def _fetch_who():
    try:
        return parse_who(get_json(WHO))
    except Exception:  # noqa: BLE001 - some deployments reject OData options
        return parse_who(get_json(WHO_FALLBACK))


def _fetch_markets():
    out = {}
    for name, sym in CONFIG["markets"]["symbols"].items():
        try:
            out[name] = market_quote(sym)
        except Exception as e:  # noqa: BLE001
            out[name] = {"error": str(e)[:120]}
    if all("error" in v for v in out.values()):
        raise RuntimeError("all market quotes failed")
    return out
