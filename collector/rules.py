"""Turn fetched data into alerts. Pure functions: easy to test offline."""
from datetime import datetime, timedelta, timezone
from statistics import median

from common import CONFIG, in_box, iso

ANOM = CONFIG["anomaly"]
METRIC_LABELS = {
    "bizjets_airborne": "long-range private jets airborne near major hubs",
    "retreat_bizjets": "long-range private jets at NZ / Hawaii / Patagonia",
    "mil_total": "military aircraft airborne",
    "tankers": "air-refuelling tankers airborne",
    "nc3": "US nuclear command aircraft (E-4B / E-6B) airborne",
}


def _alert(key, channels, category, title, message, priority=3, confirmed=True, tags=None):
    return {"key": key, "channels": channels, "category": category, "title": title, "message": message,
            "priority": priority, "confirmed": confirmed, "tags": tags or []}


def _parse_ts(s):
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def baseline(series, metric, now):
    """Median and robust spread of `metric` at a similar hour of day over the baseline window."""
    cutoff = now - timedelta(days=ANOM["baseline_days"])
    vals = []
    for row in series:
        v = row.get(metric)
        if v is None:
            continue
        ts = _parse_ts(row["ts"])
        if ts < cutoff or ts >= now - timedelta(minutes=5):
            continue
        dh = abs(ts.hour + ts.minute / 60 - (now.hour + now.minute / 60))
        if min(dh, 24 - dh) <= ANOM["hour_window"]:
            vals.append(v)
    if len(vals) < ANOM["min_samples"]:
        return None
    med = median(vals)
    mad = median(abs(v - med) for v in vals) * 1.4826
    return {"median": med, "spread": max(mad, 1.0), "n": len(vals)}


def anomalies(current, series, now):
    """Return {metric: info} for every tracked metric, flagging anomalous ones."""
    out = {}
    for metric in METRIC_LABELS:
        v = current.get(metric)
        if v is None:
            out[metric] = {"value": None, "status": "no data"}
            continue
        b = baseline(series, metric, now)
        if not b:
            out[metric] = {"value": v, "status": "learning baseline"}
            continue
        z = (v - b["median"]) / b["spread"]
        ratio = v / b["median"] if b["median"] > 0 else float("inf")
        excess = v - b["median"]
        flag = (z >= ANOM["z_threshold"] and excess >= ANOM["min_excess"].get(metric, 5)
                and (ratio >= ANOM["min_ratio"] or metric == "nc3"))
        out[metric] = {"value": v, "median": round(b["median"], 1), "z": round(z, 1), "ratio": round(ratio, 2),
                       "samples": b["n"], "status": "ANOMALY" if flag else "normal"}
    return out


def evaluate(results, state, series, now):
    """Returns (alerts, state). On the very first run nothing is pushed; we only record what exists."""
    alerts = []
    bootstrap = not state.get("bootstrapped")
    seen = state.setdefault("seen", {})
    cooldown = state.setdefault("cooldown", {})
    au_rel = set(CONFIG["au_relevant_countries"])

    primed = set(state.get("primed", []))

    def new(key, src):
        """True if this item is new AND the source has been seen before (no flood on first sight)."""
        if key in seen:
            return False
        seen[key] = iso(now)
        return src in primed

    # US travel advisories: a country raised to Level 4, or a departure order
    us = results.get("us_advisories")
    if us:
        levels = state.setdefault("us_levels", {})
        for country, a in us.items():
            prev = levels.get(country)
            levels[country] = a["level"]
            rel = " (relevant to Australia)" if country in au_rel else ""
            if prev is not None and a["level"] == 4 and prev < 4 and not bootstrap:
                alerts.append(_alert(f"us-l4-{country}-{a['date']}", ["proactive"], "geo",
                                     f"US: Do Not Travel - {country}",
                                     f"US State Dept raised {country} from Level {prev} to Level 4{rel}. {a['link']}",
                                     priority=4 if rel else 3, tags=["warning"]))
            if a["departure"] and new(f"us-dep-{country}-{a['date']}", "us_advisories"):
                alerts.append(_alert(f"us-dep-{country}", ["proactive"], "geo",
                                     f"US embassy departure order - {country}",
                                     f"US advisory for {country} mentions an ordered/authorised departure of staff{rel}. {a['link']}",
                                     priority=4, tags=["rotating_light"]))

    # UK FCDO: new updates that advise against all travel / tell people to leave
    for e in results.get("uk_advice") or []:
        text = (e["title"] + " " + e["summary"]).lower()
        if any(k in text for k in CONFIG["uk_keywords"]) and new(f"uk-{e['id']}-{e['updated']}", "uk_advice"):
            alerts.append(_alert(f"uk-{e['id']}", ["proactive"], "geo", f"UK FCDO: {e['title'][:80]}",
                                 f"{e['summary'][:240]} {e['link']}", tags=["warning"]))

    # GDACS: Red alerts anywhere; Orange in our region; Red in Australia goes to both channels
    for d in results.get("gdacs") or []:
        if not d["current"] or d["alert"] not in ("Red", "Orange"):
            continue
        in_au = d["iso3"] == "AUS" or in_box(d["lat"], d["lon"], CONFIG["australia_box"])
        in_reg = in_box(d["lat"], d["lon"], CONFIG["region_box"])
        if d["alert"] == "Orange" and not in_reg:
            continue
        if new(f"gdacs-{d['id']}-{d['alert']}", "gdacs"):
            ch = ["proactive", "reactive"] if (d["alert"] == "Red" and in_au) else ["proactive"]
            alerts.append(_alert(f"gdacs-{d['id']}", ch, "nature", f"{d['alert']} disaster alert: {d['country'] or d['type']}",
                                 f"{d['title'][:220]} {d['link']}", priority=5 if in_au else 4 if d["alert"] == "Red" else 3,
                                 tags=["ocean"] if d["type"] == "TS" else ["warning"]))

    # USGS: M7+ in our region, or any M7.5+ with a tsunami flag
    for q in results.get("usgs") or []:
        mag = q["mag"] or 0
        reg = in_box(q["lat"], q["lon"], CONFIG["region_box"])
        if (mag >= 7.0 and reg) or (mag >= 7.5 and q["tsunami"]):
            if new(f"usgs-{q['id']}", "usgs"):
                tsu = " Tsunami possible - check official warnings." if q["tsunami"] else ""
                alerts.append(_alert(f"usgs-{q['id']}", ["proactive"], "nature", f"M{mag:.1f} earthquake: {q['place']}",
                                     f"Magnitude {mag:.1f} near {q['place']}.{tsu} {q['url']}", priority=4, tags=["warning"]))

    # WHO: PHEIC declarations go to both channels; worrying new outbreaks to early warning
    for w in results.get("who") or []:
        text = (w["title"] + " " + w["summary"]).lower()
        if "public health emergency of international concern" in text and "declare" in text:
            if new(f"who-pheic-{w['id']}", "who"):
                alerts.append(_alert(f"who-pheic-{w['id']}", ["proactive", "reactive"], "bio",
                                     "WHO: international public health emergency",
                                     f"{w['title']}. {w['url']}", priority=5, tags=["biohazard"]))
        elif any(k in text for k in CONFIG["who_keywords"]) or any(c.lower() in text for c in au_rel):
            if new(f"who-{w['id']}", "who"):
                alerts.append(_alert(f"who-{w['id']}", ["proactive"], "bio", f"WHO outbreak report: {w['title'][:80]}",
                                     f"{w['summary'][:220]} {w['url']}", priority=3, tags=["microbe"]))
        else:
            new(f"who-{w['id']}", "who")

    # ACSC (Australian Cyber Security Centre) alerts
    for a in results.get("acsc") or []:
        if new(f"acsc-{a['id']}", "acsc") and CONFIG["acsc_push_all"]:
            alerts.append(_alert(f"acsc-{a['id']}", ["proactive"], "cyber", f"ACSC alert: {a['title'][:90]}",
                                 a["link"], priority=3, tags=["computer"]))
    for a in results.get("cisa") or []:
        new(f"cisa-{a['id']}", "cisa")  # recorded for the analysis run; no push

    # Markets: big daily moves
    mk = results.get("markets") or {}
    mc = CONFIG["markets"]
    day = now.strftime("%Y-%m-%d")
    for name, q in mk.items():
        ch = q.get("change_pct")
        if ch is None:
            continue
        lim = mc["day_change_alert_pct"].get(name)
        hit = lim is not None and ((lim > 0 and ch >= lim) or (lim < 0 and ch <= lim))
        if name == "VIX" and q.get("price", 0) >= mc["vix_level_alert"]:
            hit = True
        if hit and new(f"mkt-{name}-{day}", "markets"):
            alerts.append(_alert(f"mkt-{name}", ["proactive"], "markets", f"Market stress: {name} {ch:+.1f}%",
                                 f"{name} at {q['price']} ({ch:+.1f}% on the day). Sharp moves like this often follow a major shock.",
                                 priority=3, tags=["chart_with_downwards_trend"]))

    # Aircraft anomalies (unconfirmed, early warning only)
    ac = results.get("aircraft") or {}
    an = anomalies(ac, series, now)
    for metric, info in an.items():
        if info["status"] != "ANOMALY":
            continue
        last = cooldown.get(metric)
        if last and now - _parse_ts(last) < timedelta(hours=ANOM["cooldown_hours"]):
            continue
        cooldown[metric] = iso(now)
        if bootstrap:
            continue
        alerts.append(_alert(f"air-{metric}", ["proactive"], "geo",
                             f"UNCONFIRMED: unusual flight activity",
                             f"{info['value']} {METRIC_LABELS[metric]} - about {info['ratio']}x normal for this time of day "
                             f"(usual ~{info['median']}). Watching for confirmation from official sources.",
                             priority=4, confirmed=False, tags=["airplane", "question"]))

    # Prune old seen keys
    cutoff = iso(now - timedelta(days=45))
    for k in [k for k, t in seen.items() if t < cutoff]:
        del seen[k]
    state["primed"] = sorted(primed | {k for k, v in results.items() if v})
    state["bootstrapped"] = True
    return alerts, state, an
