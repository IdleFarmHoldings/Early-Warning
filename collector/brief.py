"""data/brief.json: a short version of latest.json + indicators.json for the twice-daily analysis.

latest.json has grown past 150 KB, which makes the analysis slow and expensive to read. The brief
keeps only what the analysis prompt asks for, drops links and raw samples, and flags what is
abnormal, so a quiet day reads as a few short lists. latest.json is unchanged for the board.
"""


def _drop(row):
    """Worst drop_pct across the outage-monitor datasets for one network or country."""
    vals = [row[ds]["drop_pct"] for ds in ("ping-slash24", "bgp")
            if isinstance(row.get(ds), dict) and row[ds].get("drop_pct") is not None]
    return max(vals) if vals else None


# Attack topics get 5 headlines, not 3: the analysis missed the 19 and 24 Nov 2026 Houthi strikes on Riyadh's
# tallest tower, and 3 out of 15 easily hides a new attack behind repeats of an older story.
ATTACK_TOPICS = {"strikes", "shipping_attacks", "gulf_strikes"}


def _titles(items, n, key="title"):
    return [i.get(key) for i in (items or [])[:n] if isinstance(i, dict) and i.get(key)]


def _chan(c):
    """4chan bursts only (tip-offs to check, never evidence). Quiet topics are left out."""
    if not isinstance(c, dict):
        return None
    bursts = [{"topic": t.get("name"), "new_threads": t.get("new_threads"), "usual": t.get("usual"), "why": t.get("why"),
               "threads": [f"/{x.get('board')}/ {x.get('title')} ({x.get('replies')} replies in {x.get('age_h')} h) {x.get('url')}"
                           for x in (t.get("threads") or [])]}
              for t in (c.get("topics") or {}).values() if t.get("status") == "BURST"]
    return {"boards": c.get("boards"), "bursts": bursts}


def build(latest, indicators, campaigns=None):
    L = latest or {}
    loc = L.get("local_sa") or {}
    ls = loc.get("summary") or {}
    ac = (L.get("aircraft") or {}).get("anomalies") or {}
    ns = L.get("national_shocks") or {}
    shocks_traffic = {c: t.get("status") for c, t in (ns.get("traffic") or {}).items()
                      if t.get("status") not in ("normal", "learning baseline")}
    news = {}
    for topic, items in ((L.get("news_watch") or {}).items() if isinstance(L.get("news_watch"), dict) else []):
        t = _titles(items.get("items") if isinstance(items, dict) else items, 5 if topic in ATTACK_TOPICS else 3)
        if t:
            news[topic] = t
    return {
        "generated_at": L.get("generated_at"),
        "about": "Short form of latest.json + indicators.json for the analysis run. Links and raw samples removed.",
        "failed_sources": {k: (v.get("error") or "")[:120] for k, v in (L.get("source_health") or {}).items()
                           if isinstance(v, dict) and not v.get("ok", True)},
        "recent_alerts": [{"title": a.get("title"), "at": a.get("sent_at") or a.get("at"),
                           "channels": a.get("channels") or ([a["channel"]] if a.get("channel") else []),
                           "confirmed": a.get("confirmed"), "message": (a.get("message") or "")[:110]}
                          for a in (L.get("recent_alerts") or [])[:20]],
        "aircraft_flags": {k: {kk: v.get(kk) for kk in ("status", "value", "median")}
                           for k, v in ac.items() if v.get("status") in ("ANOMALY", "DROP")},
        "watching": [{k: w.get(k) for k in ("what", "region", "text", "first_seen", "last_seen", "active")}
                     for w in (L.get("watching") or [])],
        "nc3": (ac.get("nc3") or {}).get("status"),
        "tankers": (ac.get("tankers") or {}).get("status"),
        "national_shocks": {"shocks": ns.get("shocks") or [], "faa": {k: (ns.get("faa") or {}).get(k)
                            for k in ("airports", "national", "ground_stops")},
                            "traffic_not_normal": shocks_traffic, "tracking_glitch": ns.get("tracking_glitch")},
        "incidents": [{"target": i.get("target"), "severity": i.get("severity"), "confirmed": i.get("confirmed"),
                       "attack_reported": bool(i.get("attack")),
                       # would have been an emergency, but only news says so: the analysis should confirm or rule out
                       "needs_confirming": bool(i.get("news_only_critical")),
                       "headlines": _titles(i.get("headlines"), 3)}
                      for i in sorted(L.get("incidents") or [], key=lambda i: not i.get("attack"))],
        "cyber_campaigns": [{**{k: c.get(k) for k in ("title", "actor", "score", "band", "trend", "countries", "sectors",
                                                     "techniques", "incidents_7d", "touches_australia", "official_warning",
                                                     "attributed_officially")},
                             "incidents": [f"{i['date']} {'/'.join(i['countries']) or '?'}: {i['title']} ({i['linked_by']})"
                                           for i in (c.get("incidents") or [])[:5]],
                             "hypothesis": {k: (c.get("hypothesis") or {}).get(k) for k in ("if_deliberate", "next_targets",
                                                                                            "confirm_if", "other_explanations")}}
                            for c in ((campaigns or {}).get("campaigns") or [])[:4]],
        "us_levels_au_relevant": L.get("us_levels_au_relevant"),
        "us_departure_orders": L.get("us_departure_orders"),
        "gdacs_orange_red": _titles(L.get("gdacs_current_orange_red"), 5),
        "quakes_m6_24h": L.get("quakes_m6_24h") or [],
        "who": list(dict.fromkeys(_titles([{"title": t} if isinstance(t, str) else t
                                           for t in (L.get("who_latest") or [])], 8))),
        "acsc": _titles([{"title": t} if isinstance(t, str) else t for t in (L.get("acsc_latest") or [])], 5),
        "cisa": _titles([{"title": t} if isinstance(t, str) else t for t in (L.get("cisa_latest") or [])], 5),
        # big_move: a large daily move; the collector no longer pushes these, mention in the digest if it matters
        "markets": {k: {"price": v.get("price"), "change_pct": v.get("change_pct"), "big_move": bool(v.get("big_move"))}
                    for k, v in (L.get("markets") or {}).items()},
        "telecom_drop_pct": {k: _drop(v) for k, v in (L.get("telecom") or {}).items()},
        "internet_drop_pct": {k: _drop(v) for k, v in (L.get("internet_countries") or {}).items()},
        "gps_bad_pct": {v.get("name", k): v.get("bad_pct") for k, v in (L.get("gps_jamming") or {}).items()},
        "cyber": {k: (L.get("cyber") or {}).get(k) for k in ("infocon", "c2")} |
                 {"kev": {k: v for k, v in ((L.get("cyber") or {}).get("kev") or {}).items() if k != "recent"}},
        "sectors_relative_5d": {k: v.get("relative_5d") for k, v in (L.get("sectors") or {}).items()},
        "local_sa": {
            "cfs_feed_ok": ls.get("cfs_feed_ok"), "fire_danger_ok": ls.get("fire_danger_ok"),
            "fire_danger": {d: v.get("text") for d, v in (loc.get("fire_danger") or {}).items()},
            "fire_ban_districts": ls.get("fire_ban_districts"), "cfs_warnings": ls.get("emergency_warnings"),
            "watch_and_act": ls.get("watch_and_act"), "nearby_incidents": ls.get("nearby_incidents"),
            "fires_active": ls.get("fires_active"),
            "bom": [b.get("title") for b in (loc.get("bom") or [])],
            "grid_lor_level": ls.get("grid_lor_level"), "possible_blackout": ls.get("possible_blackout"),
            "grid_demand_mw": ls.get("grid_demand_mw"), "grid_demand_usual_mw": ls.get("grid_demand_usual_mw"),
            "grid_demand_fall_pct": ls.get("grid_demand_fall_pct"), "rooftop_pv_mw": ls.get("grid_rooftop_pv_mw"),
            "power_news": _titles(ls.get("power_news"), 5), "sawater_news": _titles(ls.get("sawater_news"), 5),
            "road_hazards": sorted({f"{r.get('road')}, {r.get('suburb')}: {r.get('type')}"[:120]
                                    for r in ((loc.get("roads") or {}).get("closures") or []) if r.get("hazard")}),
            "outlook": ls.get("outlook"), "fuel": ls.get("fuel"),
            "ed_very_busy": ls.get("ed_very_busy"), "cable_watch": ls.get("cable_watch"),
        },
        "chokepoints": {k: {kk: v.get(kk) for kk in ("avg_7d", "usual_7d", "change_pct", "last_date")}
                        for k, v in (L.get("chokepoints") or {}).items()},
        "prediction_markets": [{"q": p.get("question"), "yes": p.get("yes_pct"), "d24h": p.get("change_24h")}
                               for p in (L.get("prediction_markets") or [])],
        "news_watch": news,
        "chan_chatter": _chan(L.get("chan")),
        # Providers' own status pages: first-hand, official. big = the provider's top severity.
        "status_pages": [{k: i.get(k) for k in ("provider", "title", "big", "started", "where", "au")}
                         for i in ((L.get("status_pages") or {}).get("active") or [])[:10]],
        "analyst": {src: _titles(items, 4) for src, items in (L.get("analyst_latest") or {}).items()}
        if isinstance(L.get("analyst_latest"), dict) else {},
        "scenarios": [{"key": s.get("key"), "score": s.get("score"), "band": s.get("band"), "trend": s.get("trend")}
                      | ({"domains": s.get("domains"), "convergence": (s.get("convergence") or "")[:300],
                          "lit": [{"name": x.get("name"), "detail": (x.get("detail") or "")[:160]} for x in s["lit"]]}
                         if s.get("lit") else {})
                      for s in (indicators or {}).get("scenarios") or []],
    }
