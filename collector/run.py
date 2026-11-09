"""Early Warning collector: fetch sources, detect alerts, push to ntfy, write data for the analysis run.

Run every 15 minutes by GitHub Actions. Use --dry-run to skip pushes.
"""
import argparse
import json
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import CONFIG, ROOT, append_jsonl, iso, load_json, now_utc, push, save_json  # noqa: E402
from rules import evaluate  # noqa: E402
from sources import fetch_all  # noqa: E402

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


def send(alert, dry_run):
    sent = {}
    for ch in alert["channels"]:
        sent[ch] = push(ch, alert["title"], alert["message"], priority=alert.get("priority", 3),
                        tags=alert.get("tags"), click=CONFIG["board_url"], dry_run=dry_run)
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
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    now = now_utc().replace(second=0, microsecond=0)

    state = load_json("state/state.json", {})
    first_run = not state.get("bootstrapped")
    series = load_series(now)
    results, health = fetch_all()
    alerts, state, anomalies = evaluate(results, state, series, now)

    ac = results.get("aircraft") or {}
    mk = results.get("markets") or {}
    row = {"ts": iso(now), **{k: ac.get(k) for k in ("bizjets_airborne", "retreat_bizjets", "mil_total", "tankers", "nc3", "isr")},
           **{f"mkt_{k}": v.get("price") for k, v in mk.items() if isinstance(v, dict)}}
    append_jsonl(f"data/series/{now.strftime('%Y-%m')}.jsonl", row)

    log = []
    for a in alerts:
        a["sent"] = send(a, args.dry_run)
        a["at"] = iso(now)
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
