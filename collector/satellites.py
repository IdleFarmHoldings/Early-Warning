"""Reconnaissance-satellite pass density (simple two-body + J2 propagation) and manoeuvre detection.
Elements come from CelesTrak (refreshed at most every 6 hours, as CelesTrak asks)."""
import math
import re
from datetime import datetime, timedelta, timezone

from common import CONFIG, get_json, iso

CELESTRAK = "https://celestrak.org/NORAD/elements/gp.php?GROUP=active&FORMAT=json"
MU, RE, J2 = 398600.4418, 6378.137, 1.08262668e-3
OWNERS = [(r, o) for r, o in [
    (r"^USA \d", "US"), (r"^NROL", "US"), (r"^LACROSSE", "US"),
    (r"^YAOGAN", "China"), (r"^GAOFEN", "China"), (r"^JILIN", "China"), (r"^SHIYAN", "China"), (r"^HUANJING", "China"),
    (r"^(KOSMOS|COSMOS) ", "Russia"), (r"^BARS-M", "Russia"), (r"^PERSONA", "Russia"),
    (r"^OFEQ", "Israel"), (r"^IGS", "Japan"), (r"^SAR-LUPE", "Germany"), (r"^SARAH", "Germany"),
    (r"^CSO", "France"), (r"^HELIOS", "France"), (r"^COSMO-SKYMED", "Italy"), (r"^RISAT", "India"),
    (r"^CARTOSAT", "India")]]


def owner(name):
    for pat, o in OWNERS:
        if re.match(pat, name or ""):
            return o
    return None


def _epoch(s):
    s = s.rstrip("Z")
    fmt = "%Y-%m-%dT%H:%M:%S.%f" if "." in s else "%Y-%m-%dT%H:%M:%S"
    return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)


def _gmst(t):
    jd = t.timestamp() / 86400.0 + 2440587.5
    return math.radians((280.46061837 + 360.98564736629 * (jd - 2451545.0)) % 360.0)


def subpoint(el, t):
    """Approximate sub-satellite latitude/longitude (degrees) at time t."""
    n = el["MEAN_MOTION"] * 2 * math.pi / 86400.0
    e = el["ECCENTRICITY"]
    i = math.radians(el["INCLINATION"])
    a = (MU / n ** 2) ** (1 / 3)
    p = a * (1 - e * e)
    dt = (t - el["_epoch"]).total_seconds()
    k = 1.5 * J2 * (RE / p) ** 2 * n
    raan = math.radians(el["RA_OF_ASC_NODE"]) - k * math.cos(i) * dt
    argp = math.radians(el["ARG_OF_PERICENTER"]) + 0.5 * k * (5 * math.cos(i) ** 2 - 1) * dt
    m = (math.radians(el["MEAN_ANOMALY"]) + n * dt) % (2 * math.pi)
    E = m
    for _ in range(6):
        E -= (E - e * math.sin(E) - m) / (1 - e * math.cos(E))
    nu = 2 * math.atan2(math.sqrt(1 + e) * math.sin(E / 2), math.sqrt(1 - e) * math.cos(E / 2))
    u = argp + nu
    x = math.cos(raan) * math.cos(u) - math.sin(raan) * math.sin(u) * math.cos(i)
    y = math.sin(raan) * math.cos(u) + math.cos(raan) * math.sin(u) * math.cos(i)
    z = math.sin(u) * math.sin(i)
    lat = math.degrees(math.asin(max(-1, min(1, z))))
    lon = math.degrees(math.atan2(y, x) - _gmst(t))
    return lat, (lon + 180) % 360 - 180


def _dist_km(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    c = math.sin(p1) * math.sin(p2) + math.cos(p1) * math.cos(p2) * math.cos(dl)
    return RE * math.acos(max(-1, min(1, c)))


def select(elements):
    out = []
    for el in elements:
        o = owner(el.get("OBJECT_NAME"))
        if not o or not el.get("MEAN_MOTION") or el["MEAN_MOTION"] < 11:  # imaging/radar satellites are in low orbit
            continue
        try:
            el = {**el, "_epoch": _epoch(el["EPOCH"]), "_owner": o}
        except (KeyError, ValueError):
            continue
        out.append(el)
    return out


def analyse(sats, now, prev_elements, history):
    sc = CONFIG["satellites"]
    step, hours, cell, radius = sc["step_minutes"], sc["hours_ahead"], sc["grid_deg"], sc["hotspot_radius_km"]
    grid, hot = {}, {h["key"]: {"name": h["name"], "passes": 0, "by_owner": {}} for h in sc["hotspots"]}
    for el in sats:
        inside = {h["key"]: False for h in sc["hotspots"]}
        for k in range(0, hours * 60, step):
            lat, lon = subpoint(el, now + timedelta(minutes=k))
            g = f"{int(math.floor(lat / cell) * cell)},{int(math.floor(lon / cell) * cell)}"
            grid[g] = grid.get(g, 0) + 1
            for h in sc["hotspots"]:
                near = _dist_km(lat, lon, h["lat"], h["lon"]) <= radius
                if near and not inside[h["key"]]:  # count each new pass once
                    hot[h["key"]]["passes"] += 1
                    hot[h["key"]]["by_owner"][el["_owner"]] = hot[h["key"]]["by_owner"].get(el["_owner"], 0) + 1
                inside[h["key"]] = near
    for key, h in hot.items():
        past = [r["passes"].get(key) for r in history if r.get("passes", {}).get(key) is not None]
        if len(past) >= sc["min_history"]:
            med = sorted(past)[len(past) // 2]
            h["usual"] = med
            h["ratio"] = round(h["passes"] / med, 2) if med else None
    manoeuvres = []
    for el in sats:
        prev = prev_elements.get(str(el["NORAD_CAT_ID"]))
        if prev and el["EPOCH"] != prev["epoch"]:
            dn = el["MEAN_MOTION"] - prev["n"]
            if abs(dn) >= sc["manoeuvre_dn"]:
                n1 = prev["n"] * 2 * math.pi / 86400.0
                n2 = el["MEAN_MOTION"] * 2 * math.pi / 86400.0
                da = (MU / n2 ** 2) ** (1 / 3) - (MU / n1 ** 2) ** (1 / 3)
                manoeuvres.append({"name": el["OBJECT_NAME"], "owner": el["_owner"], "norad": el["NORAD_CAT_ID"],
                                   "altitude_change_km": round(da, 1), "epoch": el["EPOCH"]})
    by_owner = {}
    for el in sats:
        by_owner[el["_owner"]] = by_owner.get(el["_owner"], 0) + 1
    return {
        "generated_at": iso(now), "hours_ahead": hours, "step_minutes": step, "grid_deg": cell,
        "satellites": len(sats), "by_owner": by_owner,
        "grid": grid, "hotspots": hot, "manoeuvres": manoeuvres[:40],
    }


def refresh(now, state, previous_output):
    """Fetch and analyse if the last run is older than the refresh interval. Returns (output or None, state)."""
    sc = CONFIG["satellites"]
    last = (previous_output or {}).get("generated_at")
    if last and now - datetime.strptime(last, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc) < timedelta(hours=sc["refresh_hours"]):
        return None, state
    sats = select(get_json(CELESTRAK, timeout=60))
    if not sats:
        raise RuntimeError("no reconnaissance satellites found in CelesTrak data")
    hist = state.get("sat_history", [])
    out = analyse(sats, now, state.get("sat_prev", {}), hist)
    state["sat_prev"] = {str(el["NORAD_CAT_ID"]): {"n": el["MEAN_MOTION"], "epoch": el["EPOCH"]} for el in sats}
    state["sat_history"] = (hist + [{"at": iso(now), "passes": {k: v["passes"] for k, v in out["hotspots"].items()}}])[-60:]
    return out, state
