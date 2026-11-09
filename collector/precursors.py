"""Precursor playbook: score how many known warning signs are showing per scenario, keep an event journal,
and learn which signs actually came before real events (lead times). Pure functions where possible."""
import json
from datetime import timedelta
from statistics import median

from common import ROOT, iso

PLAYBOOK = json.loads((ROOT / "collector" / "playbook.json").read_text())


def band(score):
    name = PLAYBOOK["bands"][0][1]
    for lo, n in PLAYBOOK["bands"]:
        if score >= lo:
            name = n
    return name


def _lit(ind, ctx):
    """Return a short detail string if the indicator is showing, else None."""
    t = ind["type"]
    if t == "anomaly":
        hits = []
        for m in ind["metrics"]:
            a = ctx["anomalies"].get(m) or {}
            if a.get("status") in ("ANOMALY", "DROP"):
                hits.append(f"{m} {a.get('value')} vs usual {a.get('median')}")
        return "; ".join(hits) or None
    if t == "internet":
        hits = []
        for c in ind["countries"]:
            row = (ctx["internet"] or {}).get(c) or {}
            d = [row[ds]["drop_pct"] for ds in ("ping-slash24", "bgp") if isinstance(row.get(ds), dict)
                 and row[ds].get("drop_pct") is not None and (row[ds].get("baseline") or 0) >= 50]
            if d and max(d) >= ind["min"]:
                hits.append(f"{c} {round(max(d))}% offline")
        return "; ".join(hits) or None
    if t == "telecom":
        hits = []
        for name, row in (ctx["telecom"] or {}).items():
            d = [row[ds]["drop_pct"] for ds in ("ping-slash24", "bgp") if isinstance(row.get(ds), dict)
                 and row[ds].get("drop_pct") is not None and (row[ds].get("baseline") or 0) >= 50]
            if d and max(d) >= ind["min"]:
                hits.append(f"{name} {round(max(d))}% down")
        return "; ".join(hits) if len(hits) >= ind.get("min_count", 1) else None
    if t == "departure":
        hits = [c for c in ctx["departures"] if c in ind["countries"]]
        return ("Departure order: " + ", ".join(hits)) if hits else None
    if t == "news":
        keys = ind["key"] if isinstance(ind["key"], list) else [ind["key"]]
        for k in keys:
            for it in ((ctx["news"] or {}).get(k) or {}).get("items", []):
                title = it.get("title", "")
                if any(w.lower() in title.lower() for w in ind["words"]):
                    return title[:140]
        return None
    if t == "market":
        q = (ctx["markets"] or {}).get(ind["name_mkt"]) or {}
        if "change_at_least" in ind and (q.get("change_pct") or 0) >= ind["change_at_least"]:
            return f"{ind['name_mkt']} {q['change_pct']:+.1f}%"
        if "price_at_least" in ind and (q.get("price") or 0) >= ind["price_at_least"]:
            return f"{ind['name_mkt']} at {q['price']}"
        return None
    if t == "sat":
        hits = []
        for h in ind["hotspots"]:
            s = ((ctx["satellites"] or {}).get("hotspots") or {}).get(h) or {}
            if s.get("ratio") and s["ratio"] >= ind["ratio"]:
                hits.append(f"{s.get('name', h)} {s['ratio']}x usual passes")
        return "; ".join(hits) or None
    if t == "acsc":
        return ctx["acsc_new"][0][:140] if ctx["acsc_new"] else None
    return None


def score_scenarios(ctx, prev_scores):
    out = []
    for sc in PLAYBOOK["scenarios"]:
        lit, unlit, score = [], [], 0
        for ind in sc["indicators"]:
            d = _lit(ind, ctx)
            if d:
                lit.append({"id": ind["id"], "name": ind["name"], "detail": d, "weight": ind["weight"]})
                score += ind["weight"]
            else:
                unlit.append({"id": ind["id"], "name": ind["name"], "weight": ind["weight"]})
        score = min(100, score)
        prev = prev_scores.get(sc["key"])
        trend = "steady" if prev is None or abs(score - prev) < 5 else "up" if score > prev else "down"
        out.append({"key": sc["key"], "name": sc["name"], "score": score, "band": band(score), "trend": trend,
                    "lit": lit, "unlit": unlit, "why": sc["why"], "sa_impact": sc["sa_impact"]})
    return out


def lit_ids(scenarios):
    return sorted(f"{s['key']}.{i['id']}" for s in scenarios for i in s["lit"])


def journal_events(status, journal, series, now):
    """Record newly reported real-world events with the warning signs seen in the lookback window before them."""
    known = {e["eventKey"] for e in journal}
    new = []
    reported = list(status.get("events_observed") or []) + [
        {**c, "scenario": c.get("scenario", "ausinfra")} for c in status.get("confirmed") or []]
    cutoff = iso(now - timedelta(hours=PLAYBOOK["lookback_hours"]))
    for ev in reported:
        key = ev.get("eventKey")
        if not key or key in known:
            continue
        first_seen = {}
        for row in series:
            if row.get("ts", "") < cutoff:
                continue
            for i in row.get("lit") or []:
                first_seen.setdefault(i, row["ts"])
        rec = {"eventKey": key, "title": ev.get("title", ""), "scenario": ev.get("scenario"), "date": ev.get("date"),
               "journaled_at": iso(now), "precursors_first_seen": first_seen}
        new.append(rec)
        known.add(key)
    return new


def learn_patterns(journal, now):
    """Which warning signs preceded past events of each scenario, how often, and how far ahead."""
    out = {}
    for sc in PLAYBOOK["scenarios"]:
        evs = [e for e in journal if e.get("scenario") == sc["key"]]
        stats = []
        for ind in sc["indicators"]:
            iid = f"{sc['key']}.{ind['id']}"
            leads = []
            for e in evs:
                fs = e["precursors_first_seen"].get(iid)
                if fs:
                    t_ev = e.get("journaled_at")
                    try:
                        from datetime import datetime
                        dt = (datetime.strptime(t_ev, "%Y-%m-%dT%H:%M:%SZ") - datetime.strptime(fs, "%Y-%m-%dT%H:%M:%SZ"))
                        leads.append(round(dt.total_seconds() / 3600, 1))
                    except (TypeError, ValueError):
                        pass
            stats.append({"id": ind["id"], "name": ind["name"],
                          "seen_before": len(leads), "of_events": len(evs),
                          "median_lead_hours": median(leads) if leads else None})
        out[sc["key"]] = {"name": sc["name"], "events": len(evs), "indicators": stats}
    return {"generated_at": iso(now), "scenarios": out}
