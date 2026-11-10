"""Two early signals:
- Shipping chokepoints (IMF PortWatch): daily ship transits through Hormuz, Malacca, Bab el-Mandeb, Taiwan Strait and
  others. A sharp fall is a direct sign of supply disruption. PortWatch updates weekly, so it lags a few days.
- Prediction markets (Polymarket): crowd probabilities for conflict questions. A sudden jump often reflects
  information that has not reached the news yet."""
import json
import urllib.parse
from datetime import datetime, timedelta, timezone

from common import CONFIG, get_json, iso

PORTWATCH = ("https://services9.arcgis.com/weJ1QsnbMYJlCHdG/ArcGIS/rest/services/Daily_Chokepoints_Data/FeatureServer/0/query"
             "?where={where}&outFields=date,portname,n_total,n_tanker&orderByFields=date%20DESC&resultRecordCount=1000&f=json")
POLY = "https://gamma-api.polymarket.com/public-search?q={q}&limit_per_type=20&events_status=active"


def _day(v):
    if isinstance(v, (int, float)):
        return datetime.fromtimestamp(v / 1000, tz=timezone.utc).date().isoformat()
    return str(v)[:10]


def chokepoint_stats(rows, week=7, base_weeks=8):
    """Last 7-day average transits vs the median 7-day average of the previous 8 weeks."""
    by = {}
    for r in rows:
        by.setdefault(r["portname"], []).append((r["date"], r.get("n_total") or 0, r.get("n_tanker") or 0))
    out = {}
    for name, vals in by.items():
        vals.sort()
        if len(vals) < week * 3:
            continue
        last = vals[-week:]
        avg = sum(v[1] for v in last) / week
        tank = sum(v[2] for v in last) / week
        bases = []
        for w in range(1, base_weeks + 1):
            chunk = vals[-week * (w + 1):-week * w]
            if len(chunk) == week:
                bases.append(sum(v[1] for v in chunk) / week)
        bases.sort()
        base = bases[len(bases) // 2] if bases else None
        out[name] = {"last_date": last[-1][0], "avg_7d": round(avg, 1), "tankers_7d": round(tank, 1),
                     "usual_7d": round(base, 1) if base else None,
                     "change_pct": round((avg - base) / base * 100, 1) if base else None,
                     "daily": [{"date": v[0], "n": v[1]} for v in vals[-60:]]}
    return out


def fetch_chokepoints():
    names = CONFIG["chokepoints"]
    where = urllib.parse.quote("portname IN (" + ",".join("'" + n.replace("'", "''") + "'" for n in names) + ")"
                               + " AND date >= CURRENT_DATE - 75")
    data = get_json(PORTWATCH.format(where=where), timeout=60)
    if data.get("error"):
        where = urllib.parse.quote("portname IN (" + ",".join("'" + n + "'" for n in names) + ")")
        data = get_json(PORTWATCH.format(where=where), timeout=60)
    rows = [{**f["attributes"], "date": _day(f["attributes"]["date"])} for f in data.get("features", [])]
    if not rows:
        raise RuntimeError("PortWatch returned no rows: " + json.dumps(data)[:200])
    return chokepoint_stats(rows)


def _yes_price(m):
    try:
        outcomes = json.loads(m.get("outcomes") or "[]")
        prices = [float(x) for x in json.loads(m.get("outcomePrices") or "[]")]
    except (ValueError, TypeError):
        return None
    if len(outcomes) == 2 and len(prices) == 2 and str(outcomes[0]).lower() == "yes":
        return round(prices[0] * 100, 1)
    return None


def fetch_markets(state, now):
    pc = CONFIG["prediction_markets"]
    hist = state.setdefault("pm_hist", {})
    out = {}
    for topic in pc["topics"]:
        try:
            data = get_json(POLY.format(q=urllib.parse.quote(topic["q"])), timeout=25)
        except Exception:  # noqa: BLE001
            continue
        for ev in data.get("events") or []:
            for m in ev.get("markets") or []:
                q = m.get("question") or ""
                if m.get("closed") or not m.get("active", True):
                    continue
                if not any(w.lower() in q.lower() for w in topic["must"]):
                    continue
                vol = float(m.get("volumeNum") or m.get("volume") or 0)
                p = _yes_price(m)
                if p is None or vol < pc["min_volume"]:
                    continue
                key = m.get("conditionId") or m.get("slug") or q
                h = hist.setdefault(key, [])
                h.append([iso(now), p])
                cutoff = iso(now - timedelta(days=4))
                hist[key] = [x for x in h if x[0] >= cutoff][-400:]
                day_ago = [x for x in hist[key] if x[0] <= iso(now - timedelta(hours=20))]
                prev = day_ago[-1][1] if day_ago else None
                out[key] = {"topic": topic["key"], "question": q[:160], "yes_pct": p, "volume": round(vol),
                            "change_24h": round(p - prev, 1) if prev is not None else None,
                            "url": f"https://polymarket.com/event/{ev.get('slug', '')}"}
    for k in [k for k in hist if k not in out and (not hist[k] or hist[k][-1][0] < iso(now - timedelta(days=4)))]:
        del hist[k]
    if not out:
        raise RuntimeError("no matching prediction markets found")
    ranked = sorted(out.values(), key=lambda x: -x["volume"])
    return {"markets": ranked[:40]}
