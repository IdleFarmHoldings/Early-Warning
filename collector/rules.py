"""Turn fetched data into alerts. Pure functions: easy to test offline."""
import math
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
    "airlift": "military transport aircraft (C-17, C-130, A400M...) airborne",
    "raaf": "RAAF aircraft broadcasting",
}
REGIONS = CONFIG["jets"]["mil_regions"]
for _r in REGIONS:
    METRIC_LABELS[f"mil_{_r['key']}"] = f"military aircraft visible over {_r['name']}"
    METRIC_LABELS[f"dark_{_r['key']}"] = f"military aircraft that vanished from tracking at altitude over {_r['name']}"
DROP_METRICS = {f"mil_{r['key']}" for r in REGIONS}
for _r in CONFIG["gps"]["regions"]:
    METRIC_LABELS[f"gps_{_r['key']}"] = f"% of aircraft over {_r['name']} reporting degraded GPS"
for _a in CONFIG["navwarn"]["areas"]:
    METRIC_LABELS[f"navwarn_{_a['key']}"] = f"active missile / live-fire navigation warnings for {_a['name']}"


def extra_metrics(results):
    """GPS jamming and navigation-warning metrics, in the same shape as the aircraft counts."""
    m = {}
    for k, r in ((results.get("gps") or {}).get("regions") or {}).items():
        m[f"gps_{k}"] = r.get("bad_pct")
    for k, n in ((results.get("navwarn") or {}).get("counts") or {}).items():
        m[f"navwarn_{k}"] = n
    return m


def _min_excess(metric):
    me = ANOM["min_excess"]
    if metric in me:
        return me[metric]
    for prefix in ("mil_", "dark_", "gps_", "navwarn_"):
        if metric.startswith(prefix):
            return me.get({"mil_": "region"}.get(prefix, prefix[:-1]), 5)
    return 5


def count_dark(prev, positions, min_alt):
    """Aircraft airborne at altitude in a region last run that are now gone from the whole military feed."""
    out = {f"dark_{r['key']}": 0 for r in REGIONS}
    for hx, (lat, lon, alt) in prev.items():
        if hx in positions or not isinstance(alt, (int, float)) or alt < min_alt:
            continue
        for r in REGIONS:
            b = r["box"]
            if lat is not None and b[0] <= lat <= b[1] and b[2] <= lon <= b[3]:
                out[f"dark_{r['key']}"] += 1
    return out


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
        flag = (z >= ANOM["z_threshold"] and excess >= _min_excess(metric)
                and (ratio >= ANOM["min_ratio"] or metric == "nc3"))
        drop = (metric in DROP_METRICS and b["median"] >= ANOM["drop_min_median"]
                and ratio <= ANOM["drop_max_ratio"] and z <= -ANOM["z_threshold"])
        # A zero baseline gives an infinite ratio; store it as null because JSON has no Infinity (browsers reject the file).
        out[metric] = {"value": v, "median": round(b["median"], 1), "z": round(z, 1),
                       "ratio": round(ratio, 2) if math.isfinite(ratio) else None,
                       "samples": b["n"], "status": "ANOMALY" if flag else "DROP" if drop else "normal"}
    return out


GLOBAL_KINDS = {"tankers": "flights", "airlift": "flights", "mil_total": "flights", "nc3": "nc3",
                "bizjets_airborne": "jets", "retreat_bizjets": "jets"}
KIND_WORDS = {"gps": "GPS jamming", "flights": "unusual military flights", "dark": "military aircraft going dark",
              "navwarn": "missile / live-fire warnings", "nc3": "US nuclear-command aircraft activity",
              "jets": "unusual private-jet movements", "internet": "an internet blackout"}


def _signal_of(metric, info):
    """(kind, region) for an anomalous metric. Kinds are independent sensor families."""
    if metric in GLOBAL_KINDS:
        return GLOBAL_KINDS[metric], "global"
    if metric == "raaf":
        return "flights", "auspng"
    for prefix, kind in (("mil_", "flights"), ("dark_", "dark"), ("navwarn_", "navwarn")):
        if metric.startswith(prefix):
            region = metric[len(prefix):]
            return ("dark" if info["status"] == "DROP" else kind), {"australia": "auspng"}.get(region, region)
    return None, None


def watch_signals(results, an, state, now, bootstrap):
    """Hold single-sensor early signs on a watch list; alert only when different kinds line up.

    One sensor on its own (GPS jamming, a burst of military flights, aircraft vanishing from tracking, a jump in
    navigation warnings) is usually an exercise, routine jamming or a tracking gap, so it is recorded in
    state["watch"] and shown on the board, not pushed. A phone alert goes out when 2+ different kinds of sign
    are active in the same region within the window - the pattern seen before real operations - or for the few
    signs that matter on their own at home (sustained GPS jamming over SA / NSW).
    """
    wc, gcf = CONFIG["watch"], CONFIG["gps_confirm"]
    held = state.setdefault("watch", {})
    cooldown = state.setdefault("cooldown", {})
    streaks = state.setdefault("gps_streak", {})
    regions = (results.get("gps") or {}).get("regions") or {}
    seen_now = {}

    # GPS: only a solid, sustained reading counts as a sign at all
    for key, region in gcf["regions"].items():
        info, reg = an.get(f"gps_{key}") or {}, regions.get(key) or {}
        if info.get("status") == "no data" or info.get("value") is None:
            continue  # a gap in coverage neither builds nor breaks a streak
        solid = info.get("status") == "ANOMALY" and (reg.get("aircraft") or 0) >= gcf["min_aircraft"]
        streaks[key] = streaks.get(key, 0) + 1 if solid else 0
        if streaks[key] >= gcf["sustain_runs"]:
            name = reg.get("name") or key
            seen_now[f"gps_{key}"] = ("gps", region, f"GPS jamming over {name}: {info['value']}% of {reg.get('aircraft')} "
                                      f"aircraft affected for {gcf['sustain_runs'] * 15}+ min (usual ~{info['median']}%)")
    # Aircraft and navigation-warning anomalies
    for metric, info in an.items():
        if metric.startswith("gps_") or info.get("status") not in ("ANOMALY", "DROP"):
            continue
        kind, region = _signal_of(metric, info)
        if not kind:
            continue
        only = "Only " if info["status"] == "DROP" else ""
        seen_now[metric] = (kind, region, f"{only}{info['value']} {METRIC_LABELS[metric]} (usual ~{info['median']})")
    # Country internet blackouts (these also push on their own, below)
    for country, region in wc["internet"].items():
        row = ((results.get("internet") or {}).get("countries") or {}).get(country) or {}
        drops = [row[ds]["drop_pct"] for ds in ("ping-slash24", "bgp")
                 if isinstance(row.get(ds), dict) and row[ds].get("drop_pct") is not None]
        if drops and max(drops) >= CONFIG["internet_country_drop_pct"]:
            seen_now[f"net_{country}"] = ("internet", region, f"Internet blackout in {country}: about {round(max(drops))}% down")

    for key, (kind, region, text) in seen_now.items():
        h = held.setdefault(key, {"first": iso(now)})
        h.update({"kind": kind, "region": region, "text": text, "last": iso(now)})
    for key in [k for k, h in held.items() if now - _parse_ts(h["last"]) > timedelta(hours=wc["expire_hours"])]:
        del held[key]

    out = []
    window = timedelta(hours=wc["window_hours"])
    active = {k: h for k, h in held.items() if now - _parse_ts(h["last"]) <= window}
    # 1. Signs that matter on their own at home
    for key in wc["alone"]:
        if key in seen_now:
            last = cooldown.get(key)
            if last and now - _parse_ts(last) < timedelta(hours=wc["cooldown_hours"]):
                continue
            cooldown[key] = iso(now)
            if not bootstrap:
                out.append(_alert(f"air-{key}", ["proactive"], "cyber", "UNCONFIRMED: GPS jamming over Australia",
                                  f"{seen_now[key][2]}. GPS jamming here would be new and serious; it can also be a "
                                  "fault. Watching for confirmation.", priority=4, confirmed=False, tags=["satellite", "question"]))
    # 2. Different kinds of sign lining up in one region
    by_region = {}
    for h in active.values():
        by_region.setdefault(h["region"], []).append(h)
    glob = by_region.pop("global", [])
    if len({h["kind"] for h in glob}) >= 2:
        by_region["global"] = []
    conv = state.setdefault("watch_alerted", {})
    for region, own in by_region.items():
        signs = own + glob
        kinds = sorted({h["kind"] for h in signs})
        if len(kinds) < 2:
            continue
        prev = conv.get(region) or {}
        recent = prev.get("at") and now - _parse_ts(prev["at"]) < timedelta(hours=wc["cooldown_hours"])
        if recent and len(kinds) <= prev.get("kinds", 0):
            continue
        conv[region] = {"at": iso(now), "kinds": len(kinds)}
        if bootstrap:
            continue
        name = wc["region_names"].get(region, region)
        first = {}
        for h in sorted(signs, key=lambda h: h["last"], reverse=True):
            first.setdefault(h["kind"], h["text"])
        detail = "; ".join(first[k] for k in kinds)[:230]
        out.append(_alert(f"watch-{region}", ["proactive"], "geo", f"Early signs lining up: {name}"[:90],
                          f"{len(kinds)} different kinds of sign in the last {wc['window_hours']} hours: {detail}. "
                          "Signs lining up like this have come before military action, but nothing is confirmed. Check the board.",
                          priority=4, confirmed=False, tags=["eyes"]))
    return out


def watch_list(state, now):
    """The held signs, newest first, for the board and the analysis run."""
    wc = CONFIG["watch"]
    out = []
    for key, h in (state.get("watch") or {}).items():
        out.append({"key": key, "kind": h["kind"], "what": KIND_WORDS.get(h["kind"], h["kind"]),
                    "region": wc["region_names"].get(h["region"], h["region"]), "text": h["text"],
                    "first_seen": h["first"], "last_seen": h["last"],
                    "active": now - _parse_ts(h["last"]) <= timedelta(hours=wc["window_hours"])})
    return sorted(out, key=lambda x: x["last_seen"], reverse=True)


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

    # WHO: PHEIC declarations go to both channels; worrying new outbreaks to early warning.
    # Only reports published in the last 14 days can alert: when WHO's sorted feed fails, the fallback returns
    # archive reports (Ebola 1997, polio 2005...) that look "new" because we have never seen their ids.
    who_cutoff = iso(now - timedelta(days=14))
    for w in results.get("who") or []:
        text = (w["title"] + " " + w["summary"]).lower()
        if w.get("date") and str(w["date"])[:19] < who_cutoff[:19]:
            new(f"who-{w['id']}", "who")  # remember it so it never alerts later either
            continue
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
    positions = ac.pop("positions", None)
    if positions is not None:
        prev = state.get("mil_prev")
        if prev is not None:
            ac.update(count_dark(prev, positions, CONFIG["jets"]["dark_min_alt_ft"]))
        state["mil_prev"] = positions
    an = anomalies({**ac, **extra_metrics(results)}, series, now)
    alerts.extend(watch_signals(results, an, state, now, bootstrap))

    # Telecom: a big drop in reachable networks for a major Australian carrier
    tc = CONFIG.get("telecom", {})
    tel = (results.get("telecom") or {}).get("networks") or {}
    down = state.setdefault("telco_down", {})
    hits = {}
    for name, row in tel.items():
        # ignore tiny signals (e.g. mobile networks with few probe-able addresses): too noisy to alert on
        drops = [row[ds]["drop_pct"] for ds in ("ping-slash24", "bgp")
                 if isinstance(row.get(ds), dict) and row[ds].get("drop_pct") is not None
                 and (row[ds].get("baseline") or 0) >= tc.get("min_baseline", 20)]
        if not drops:
            continue
        worst = max(drops)
        if worst >= tc["warn_drop_pct"]:
            hits[name] = worst
        elif name in down and worst <= tc["recover_drop_pct"]:
            del down[name]
            if not bootstrap:
                alerts.append(_alert(f"telco-up-{name}", ["proactive"], "cyber", f"{name} network recovering",
                                     f"{name}'s network looks to be back to normal levels. Some services may take a while to settle.",
                                     priority=3, tags=["white_check_mark"]))
    fresh = {}
    for name, worst in hits.items():
        last = cooldown.get(f"telco-{name}")
        if name in down and last and now - _parse_ts(last) < timedelta(hours=tc["cooldown_hours"]):
            continue
        down[name] = iso(now)
        cooldown[f"telco-{name}"] = iso(now)
        fresh[name] = worst
    if fresh and not bootstrap:
        major = any(d >= tc["major_drop_pct"] for d in hits.values()) or len(hits) >= 2
        names = ", ".join(f"{n} (about {round(d)}% down)" for n, d in sorted(hits.items(), key=lambda x: -x[1]))
        alerts.append(_alert("telco-" + "-".join(sorted(fresh)), ["proactive", "reactive"] if major else ["proactive"], "cyber",
                             f"{'MAJOR ' if major else ''}phone/internet outage: {', '.join(sorted(hits))}",
                             f"Outage monitors show {names}. You may lose phone signal or internet. Now: get some cash out, "
                             "fill up fuel, and agree where to meet in case you can't call. 000 may still work via another network.",
                             priority=5 if major else 4, confirmed=major, tags=["signal_strength"]))

    # Country-level internet blackouts
    lim = CONFIG["internet_country_drop_pct"]
    for name, row in ((results.get("internet") or {}).get("countries") or {}).items():
        drops = [row[ds]["drop_pct"] for ds in ("ping-slash24", "bgp")
                 if isinstance(row.get(ds), dict) and row[ds].get("drop_pct") is not None
                 and (row[ds].get("baseline") or 0) >= tc.get("min_baseline", 50)]
        if not drops or max(drops) < lim:
            continue
        key = f"net-{name}"
        last = cooldown.get(key)
        if last and now - _parse_ts(last) < timedelta(hours=tc["cooldown_hours"]):
            continue
        cooldown[key] = iso(now)
        if not bootstrap:
            alerts.append(_alert(key, ["proactive"], "geo", f"Internet blackout: {name}",
                                 f"About {round(max(drops))}% of {name}'s internet has dropped off in the last hour. "
                                 "Governments often cut access during unrest or military operations. Watching for the cause.",
                                 priority=4, confirmed=False, tags=["globe_with_meridians"]))

    # Prune old seen keys
    cutoff = iso(now - timedelta(days=45))
    for k in [k for k, t in seen.items() if t < cutoff]:
        del seen[k]
    state["primed"] = sorted(primed | {k for k, v in results.items() if v})
    state["bootstrapped"] = True
    return alerts, state, an
