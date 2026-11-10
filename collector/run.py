"""Early Warning collector: fetch sources, detect alerts, push to ntfy, write data for the analysis run.

Run every 15 minutes by GitHub Actions. Use --dry-run to skip pushes.
"""
import argparse
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import json
import re
import urllib.parse
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import CONFIG, ROOT, append_jsonl, iso, load_json, now_utc, push, save_json  # noqa: E402
from rules import extra_metrics, evaluate  # noqa: E402
from sources import fetch_all  # noqa: E402
import precursors  # noqa: E402
import maritime  # noqa: E402
import satellites  # noqa: E402

SERIES_DIR = ROOT / "data" / "series"


def load_series(now, days=16):
    rows, cutoff = [], iso(now - timedelta(days=days))
    for p in sorted(SERIES_DIR.glob("*.jsonl"))[-2:]:
        for line in p.read_text().splitlines():
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if r.get("ts", "") >= cutoff:
                rows.append(r)
    return rows


def alert_id(alert, now):
    """Stable id used to link a phone notification to its row on the family page."""
    base = alert.get("id") or alert.get("key") or "alert"
    base = re.sub(r"[^A-Za-z0-9-]+", "-", str(base)).strip("-")[:50] or "alert"
    return base if base[:8].isdigit() else f"{now:%Y%m%dT%H%M}-{base}"


def click_url(alert):
    """Tapping a notification opens the family page at that alert. The title and message ride along in the link
    so the page can show it at once, even before GitHub Pages has published the latest data."""
    base = CONFIG["board_url"]
    c = alert.get("click")
    if isinstance(c, str) and c.startswith(base):
        return c
    q = urllib.parse.urlencode({"alert": alert.get("id", ""), "t": alert.get("title", "")[:120],
                                "m": alert.get("message", "")[:300], "p": alert.get("priority", 3)})
    return f"{base}?{q}#alerts"


def send(alert, dry_run):
    alert.setdefault("id", alert_id(alert, now_utc()))
    sent = {}
    for ch in alert["channels"]:
        sent[ch] = push(ch, alert["title"], alert["message"], priority=alert.get("priority", 3),
                        tags=alert.get("tags"), click=click_url(alert), dry_run=dry_run)
    return sent


def relay_outbox(now, dry_run):
    """Alerts written by the Claude analysis run land in alerts/outbox/*.json; push them and archive."""
    out = []
    outbox, sent_dir = ROOT / "alerts" / "outbox", ROOT / "alerts" / "sent"
    sent_dir.mkdir(parents=True, exist_ok=True)
    for p in sorted(outbox.glob("*.json")):
        try:
            a = json.loads(p.read_text())
        except json.JSONDecodeError:
            p.rename(sent_dir / (p.stem + ".invalid.json"))
            continue
        ch = a.get("channel", "proactive")
        a["channels"] = ["proactive", "reactive"] if ch == "both" else [ch]
        a.setdefault("tags", ["memo"])
        a["id"] = alert_id({"id": p.stem}, now)
        a["sent"] = send(a, dry_run)
        a["sent_at"] = iso(now)
        a["source"] = "analysis"
        (sent_dir / p.name).write_text(json.dumps(a, indent=2) + "\n")
        p.unlink()
        out.append(a)
    return out


def summarise(results, health, anomalies, now):
    us = results.get("us_advisories") or {}
    au_rel = set(CONFIG["au_relevant_countries"])
    day_ago = (now - timedelta(days=1)).timestamp() * 1000
    return {
        "generated_at": iso(now),
        "source_health": health,
        "aircraft": {"anomalies": anomalies, "detail": results.get("aircraft")},
        "us_level4": sorted(c for c, a in us.items() if a["level"] == 4),
        "us_levels_au_relevant": {c: us[c]["level"] for c in sorted(us) if c in au_rel},
        "us_departure_orders": sorted(c for c, a in us.items() if a["departure"]),
        "uk_recent_updates": (results.get("uk_advice") or [])[:10],
        "gdacs_current_orange_red": [d for d in (results.get("gdacs") or []) if d["current"] and d["alert"] in ("Orange", "Red")],
        "quakes_m6_24h": [q for q in (results.get("usgs") or []) if (q["mag"] or 0) >= 6 and (q["time"] or 0) >= day_ago],
        "who_latest": (results.get("who") or [])[:8],
        "acsc_latest": (results.get("acsc") or [])[:6],
        "cisa_latest": (results.get("cisa") or [])[:6],
        "markets": results.get("markets"),
        "telecom": (results.get("telecom") or {}).get("networks"),
        "analyst_latest": (results.get("analysts") or {}).get("feeds"),
        "gps_jamming": (results.get("gps") or {}).get("regions"),
        "navwarn": results.get("navwarn"),
        "internet_countries": (results.get("internet") or {}).get("countries"),
        "news_watch": (results.get("news_watch") or {}).get("watch"),
        "sectors": (results.get("sectors") or {}).get("baskets"),
        "cyber": results.get("cyber"),
    }


def load_jsonl(path):
    p = ROOT / path
    if not p.exists():
        return []
    out = []
    for line in p.read_text().splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def recent_titles(items, now, hours):
    out = []
    for it in items:
        try:
            dt = parsedate_to_datetime(it.get("date") or "")
        except (TypeError, ValueError):
            continue
        if dt and dt.tzinfo and now - dt <= timedelta(hours=hours):
            out.append(it.get("title", ""))
    return out


def cable_alerts(mar, state, now, first_run):
    """Vessels from flags of interest loitering over Australian cable corridors (unconfirmed)."""
    mc = CONFIG["maritime"]
    regions = {z["key"]: z["region"] for z in mc["zones"]}
    cd = state.setdefault("cooldown", {})
    out = []
    for v in (mar or {}).get("loitering", []):
        if not v["of_interest"] or regions.get(v["zone"]) != "Australia" or v["minutes"] < mc["alert_minutes"]:
            continue
        key = f"cable-{v['mmsi']}"
        last = cd.get(key)
        if last and now - datetime.strptime(last, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc) < timedelta(hours=12):
            continue
        cd[key] = iso(now)
        if first_run:
            continue
        zname = next(z["name"] for z in mc["zones"] if z["key"] == v["zone"])
        out.append({"key": key, "channels": ["proactive"], "category": "cyber",
                    "title": "UNCONFIRMED: vessel loitering over undersea cables",
                    "message": (f"{v['flag']}-flagged vessel {v['name'] or v['mmsi']} has been slow or stopped in the {zname} "
                                f"for about {v['minutes']} min. Cable damage would disrupt internet and phone links. Watching.")[:390],
                    "priority": 4, "confirmed": False, "tags": ["anchor"]})
    return out


def precursor_alerts(scenarios, prev_ind, state, now, first_run):
    """Alert when a scenario's warning-sign score first crosses the alert line (12 h cooldown)."""
    line = precursors.PLAYBOOK["alert_score"]
    prev = {s["key"]: s["score"] for s in prev_ind.get("scenarios", [])}
    cd = state.setdefault("cooldown", {})
    out = []
    for s in scenarios:
        key = f"esc-{s['key']}"
        last = cd.get(key)
        recent = last and now - datetime.strptime(last, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc) < timedelta(hours=12)
        if s["score"] >= line and prev.get(s["key"], 0) < line and not recent and not first_run:
            cd[key] = iso(now)
            signs = "; ".join(i["name"] for i in s["lit"][:4])
            out.append({"key": key, "channels": ["proactive"], "category": "geo",
                        "title": f"Warning signs building: {s['name']}"[:90],
                        "message": f"{len(s['lit'])} known warning signs now showing ({signs}). Not confirmed - see the board for detail."[:390],
                        "priority": 4, "confirmed": False, "tags": ["warning"]})
    return out


def analytics_series(rows, now, days=14):
    """Hourly (last value per hour) history of the metrics the analytics page charts."""
    cutoff = iso(now - timedelta(days=days))
    keep = ("esc_", "gps_", "mil_", "dark_", "navwarn_", "mkt_", "rel_", "cy_")
    extra = {"airlift", "raaf", "tankers", "nc3", "bizjets_airborne", "retreat_bizjets"}
    by_hour = {}
    for r in rows:
        ts = r.get("ts", "")
        if ts < cutoff:
            continue
        by_hour[ts[:13]] = {"ts": ts, **{k: v for k, v in r.items() if k.startswith(keep) or k in extra}}
    return {"generated_at": iso(now), "rows": [by_hour[h] for h in sorted(by_hour)]}


def relay_only(now, dry_run):
    """Quick pass between full checks: push any alerts the analysis has dropped into the outbox."""
    log = relay_outbox(now, dry_run)
    if not log:
        return
    for a in log:
        append_jsonl("alerts/log.jsonl", a)
    latest = load_json("data/latest.json", {})
    latest["recent_alerts"] = (log + latest.get("recent_alerts", []))[:25]
    save_json("data/latest.json", latest)
    print(f"{iso(now)}: relayed {len(log)} alert(s) from the outbox")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--relay-only", action="store_true", help="only deliver alerts waiting in alerts/outbox")
    args = ap.parse_args()
    now = now_utc().replace(second=0, microsecond=0)
    if args.relay_only:
        relay_only(now, args.dry_run)
        return

    state = load_json("state/state.json", {})
    first_run = not state.get("bootstrapped")
    series = load_series(now)
    results, health = fetch_all()
    alerts, state, anomalies = evaluate(results, state, series, now)

    ac = results.get("aircraft") or {}
    mk = results.get("markets") or {}
    row = {"ts": iso(now), **{k: ac.get(k) for k in ("bizjets_airborne", "retreat_bizjets", "mil_total", "tankers", "nc3", "isr")},
           **{k: v for k, v in ac.items() if k.startswith(("mil_", "dark_")) or k in ("airlift", "raaf")},
           **extra_metrics(results),
           **{f"mkt_{k}": v.get("price") for k, v in mk.items() if isinstance(v, dict)}}

    # Satellites (refreshed every few hours) and the precursor playbook
    sat_out = load_json("data/satellites.json", {})
    t0 = time.time()
    try:
        fresh, state = satellites.refresh(now, state, sat_out)
        if fresh:
            sat_out = fresh
            save_json("data/satellites.json", sat_out)
        health["satellites"] = {"ok": True, "secs": round(time.time() - t0, 1), "refreshed": bool(fresh)}
    except Exception as e:  # noqa: BLE001
        health["satellites"] = {"ok": False, "error": f"{type(e).__name__}: {e}"[:300]}
    mar = {}
    t0 = time.time()
    try:
        mar = maritime.fetch_maritime(state)
        save_json("data/maritime.json", mar)
        health["maritime"] = {"ok": True, "secs": round(time.time() - t0, 1)}
    except Exception as e:  # noqa: BLE001
        health["maritime"] = {"ok": False, "error": f"{type(e).__name__}: {e}"[:300]}
    alerts += cable_alerts(mar, state, now, first_run)
    prev_ind = load_json("data/indicators.json", {})
    ctx = {
        "anomalies": anomalies,
        "internet": (results.get("internet") or {}).get("countries"),
        "telecom": (results.get("telecom") or {}).get("networks"),
        "departures": precursors.new_departures(
            sorted(c for c, a in (results.get("us_advisories") or {}).items() if a.get("departure")), state, now)
        if results.get("us_advisories") else [],
        "news": (results.get("news_watch") or {}).get("watch"),
        "markets": mk,
        "satellites": sat_out,
        "acsc_new": recent_titles(results.get("acsc") or [], now, hours=48),
        "sectors": (results.get("sectors") or {}).get("baskets") or {},
        "who": results.get("who") or [],
        "cyber": (results.get("cyber") or {}).get("summary") or {},
        "maritime": mar,
        "now": now,
    }
    scenarios = precursors.score_scenarios(ctx, {s["key"]: s["score"] for s in prev_ind.get("scenarios", [])})
    row.update({f"esc_{s['key']}": s["score"] for s in scenarios})
    row.update({f"rel_{k}": v.get("relative_5d") for k, v in ctx["sectors"].items()})
    row.update({f"cy_{k}": v for k, v in ctx["cyber"].items() if isinstance(v, (int, float))})
    row["lit"] = precursors.lit_ids(scenarios)
    append_jsonl(f"data/series/{now.strftime('%Y-%m')}.jsonl", row)
    save_json("data/indicators.json", {"generated_at": iso(now), "scenarios": scenarios,
                                       "note": "Scores count known warning signs showing now (0-100). They are not probabilities."})
    alerts += precursor_alerts(scenarios, prev_ind, state, now, first_run)

    # Event journal and learned patterns
    journal = load_jsonl("data/events.jsonl")
    for ev in precursors.journal_events(load_json("data/status.json", {}), journal, series + [row], now):
        append_jsonl("data/events.jsonl", ev)
        journal.append(ev)
    save_json("data/patterns.json", precursors.learn_patterns(journal, now))
    save_json("data/analytics.json", analytics_series(series + [row], now))

    log = []
    for a in alerts:
        a["sent"] = send(a, args.dry_run)
        a["at"] = a["sent_at"] = iso(now)
        a["source"] = "collector"
        log.append(a)
    log += relay_outbox(now, args.dry_run)
    welcomed = set(state.get("welcomed", []))
    for ch in ("proactive", "reactive"):
        if ch not in welcomed and push(ch, "Early Warning connected",
                                       f"You're subscribed to the {'Early warning' if ch == 'proactive' else 'Confirmed only'} "
                                       "channel. Alerts will arrive here.", priority=2, tags=["white_check_mark"],
                                       dry_run=args.dry_run):
            welcomed.add(ch)
    state["welcomed"] = sorted(welcomed)
    for a in log:
        append_jsonl("alerts/log.jsonl", a)

    recent = load_json("data/latest.json", {}).get("recent_alerts", [])
    summary = summarise(results, health, anomalies, now)
    summary["recent_alerts"] = (log + recent)[:25]
    save_json("data/latest.json", summary)
    save_json("state/state.json", state)

    ok = sum(1 for h in health.values() if h["ok"])
    print(f"{iso(now)}: {ok}/{len(health)} sources ok, {len(log)} alerts{' (bootstrap run)' if first_run else ''}")
    for name, h in health.items():
        if not h["ok"]:
            print(f"  FAILED {name}: {h['error']}")


if __name__ == "__main__":
    main()
