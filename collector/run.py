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
import localsa  # noqa: E402
import foresight  # noqa: E402
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
            a = _relay_one(p, sent_dir, now, dry_run)
        except Exception as e:  # noqa: BLE001 - one bad file must never stop the collector or the other alerts
            print(f"outbox: could not handle {p.name}: {type(e).__name__}: {e}")
            try:
                p.rename(sent_dir / (p.stem + ".invalid.json"))
            except OSError:
                pass
            continue
        if a:
            out.append(a)
    return out


RETRY_HOURS = 6  # keep trying an undelivered alert for this long, then file it as undelivered


def _relay_one(p, sent_dir, now, dry_run):
    """Deliver one outbox file. Returns the alert once it is finished with (delivered, or given up on);
    returns None while it is still waiting for a retry. Unusable files are set aside as *.invalid.json."""
    try:
        a = json.loads(p.read_text())
    except (json.JSONDecodeError, UnicodeDecodeError):
        a = None
    if not isinstance(a, dict) or not str(a.get("title") or "").strip() or not str(a.get("message") or "").strip():
        print(f"outbox: {p.name} is not a usable alert (needs a title and a message); set aside")
        p.rename(sent_dir / (p.stem + ".invalid.json"))
        return None
    a["title"], a["message"] = str(a["title"]).strip(), str(a["message"]).strip()
    ch = str(a.get("channel") or "proactive").strip().lower()
    wanted = ["proactive", "reactive"] if ch == "both" else [ch if ch in ("proactive", "reactive") else "proactive"]
    done = [c for c in a.get("delivered") or [] if c in wanted]  # channels that got through on an earlier try
    try:
        a["priority"] = min(5, max(1, int(a.get("priority", 3))))
    except (TypeError, ValueError):
        a["priority"] = 3
    if not isinstance(a.get("tags"), list) or not a["tags"]:
        a["tags"] = ["memo"]
    a["tags"] = [str(t) for t in a["tags"]][:5]
    a["id"] = alert_id({"id": p.stem}, now)
    a["channels"] = [c for c in wanted if c not in done]
    result = send(a, dry_run)
    a["channels"] = wanted
    a["sent"] = {**{c: True for c in done}, **result}
    failed = [c for c in wanted if not a["sent"].get(c)]
    if failed and not dry_run:
        first = a.get("first_failed_at") or iso(now)
        age = now - datetime.strptime(first, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        if age < timedelta(hours=RETRY_HOURS):
            delivered = [c for c in wanted if a["sent"].get(c)]
            if a.get("first_failed_at") != first or (a.get("delivered") or []) != delivered:
                keep = {k: v for k, v in a.items() if k not in ("channels", "sent", "id")}
                keep.update(first_failed_at=first, delivered=delivered)
                p.write_text(json.dumps(keep, indent=2) + "\n")  # written once, so retries do not create commits
            print(f"outbox: {p.name} not delivered to {', '.join(failed)}; will retry")
            return None
        a["undelivered"] = failed
        print(f"outbox: giving up on {p.name} after {RETRY_HOURS} h (not delivered to {', '.join(failed)})")
    a["sent_at"] = iso(now)
    a.setdefault("source", "analysis")
    a.pop("delivered", None)
    (sent_dir / p.name).write_text(json.dumps(a, indent=2) + "\n")
    p.unlink()
    return a


def queue_retry(alert, now):
    """A collector alert that did not reach every channel is put in the outbox so the relay keeps trying."""
    failed = [c for c in alert["channels"] if not alert["sent"].get(c)]
    if not failed:
        return None
    outbox = ROOT / "alerts" / "outbox"
    outbox.mkdir(parents=True, exist_ok=True)
    p = outbox / f"{alert['id']}-retry.json"
    p.write_text(json.dumps({
        "channel": "both" if len(failed) == 2 else failed[0], "title": alert["title"], "message": alert["message"],
        "priority": alert.get("priority", 3), "tags": alert.get("tags") or ["memo"], "source": "collector",
        "first_failed_at": iso(now)}, indent=2) + "\n")
    return p


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


CABLE_DAMAGE = r"\b(cut|severed?|damaged?|sabotaged?|snapped|broken|breaks?|fault|outage|disrupt\w*|ruptured?)\b"
CABLE_NOT_NEWS = r"\b(jailed|jails|convict\w*|sentenced|trial|court|anniversary|route|plans?|planned|proposed|to build|contract|funding)\b"
POWER_WORDS = (r"without power|blackouts?|power cuts?|power (is |was )?out\b|los(e|t|es) power|"
               r"(power|electricity|SA Power Networks)\b.*\boutages?|\boutages?\b.*\b(power|electricity|SA Power Networks)")
SA_WORDS = r"SA Power|South Australia\w*|Adelaide|\bSA\b"


def headline(item):
    """A Google News title without its trailing ' - Publisher', so a publisher's name never counts as a match."""
    t = item.get("title", "")
    return t.rsplit(" - ", 1)[0] if " - " in t else t


def power_reports(items):
    """Headlines that report a power outage in South Australia (both the place and the outage must be in the headline)."""
    out = []
    for i in items or []:
        t = headline(i)
        if re.search(POWER_WORDS, t, re.I) and re.search(SA_WORDS, t):
            out.append(t[:140])
    return out[:3]


def cable_alerts(mar, state, now, first_run, telecom=None, news=None):
    """Two stages. WATCH: a vessel from a flag of interest loitering over an Australian cable corridor goes on a
    watch list (dashboard only, no phone alert). ESCALATE: if a watched vessel was seen in the last 12 hours AND
    there is a comms symptom (an Australian carrier down 10%+, or reports of cable damage near Australia),
    send one alert."""
    mc = CONFIG["maritime"]
    regions = {z["key"]: z["region"] for z in mc["zones"]}
    names = {z["key"]: z["name"] for z in mc["zones"]}
    watch = state.setdefault("cable_watch", {})
    for v in (mar or {}).get("loitering", []):
        if v["of_interest"] and regions.get(v["zone"]) == "Australia" and v["minutes"] >= mc["alert_minutes"]:
            w = watch.setdefault(str(v["mmsi"]), {"first": iso(now)})
            w.update({"last": iso(now), "name": v["name"] or str(v["mmsi"]), "flag": v["flag"], "zone": names[v["zone"]],
                      "minutes": v["minutes"]})
    cutoff = iso(now - timedelta(hours=12))
    for m in [m for m, w in watch.items() if w["last"] < cutoff]:
        del watch[m]
    if not watch:
        return []
    symptoms = []
    for name, row in (telecom or {}).items():
        d = [row[ds]["drop_pct"] for ds in ("ping-slash24", "bgp") if isinstance(row.get(ds), dict)
             and row[ds].get("drop_pct") is not None and (row[ds].get("baseline") or 0) >= 50]
        if d and max(d) >= mc.get("comms_drop_pct", 10):
            symptoms.append(f"{name} network down about {round(max(d))}%")
    for it in ((news or {}).get("cables") or {}).get("items", []):
        t = it.get("title", "").rsplit(" - ", 1)[0]
        # must report damage to a communications cable, not merely mention one (e.g. plans for a new power cable)
        if (re.search(r"\bcables?\b", t, re.I) and re.search(CABLE_DAMAGE, t, re.I) and not re.search(CABLE_NOT_NEWS, t, re.I)
                and re.search(r"Australia|Australian|Pacific|Sydney|Perth|Darwin|Tasmania|Indonesia|Singapore|Guam", t)):
            symptoms.append(f"report: {t[:90]}")
            break
    if not symptoms:
        return []
    cd = state.setdefault("cooldown", {})
    last = cd.get("cable-escalation")
    if last and now - datetime.strptime(last, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc) < timedelta(hours=12):
        return []
    cd["cable-escalation"] = iso(now)
    if first_run:
        return []
    v = sorted(watch.values(), key=lambda w: w["last"])[-1]
    return [{"key": "cable-escalation", "channels": ["proactive"], "category": "cyber",
             "title": "UNCONFIRMED: possible undersea cable incident",
             "message": (f"{v['flag']}-flagged vessel {v['name']} loitered in the {v['zone']}, and now: {'; '.join(symptoms[:2])}. "
                         "Internet and phone services may be disrupted; keep phones charged and some cash on hand.")[:390],
             "priority": 4, "confirmed": False, "tags": ["anchor"]}]


CRITICAL_FEEDS = {
    "cfs": ("CFS bushfire warnings", "Check cfs.sa.gov.au or the Alert SA app for bushfire warnings until it is back."),
    "fdr": ("fire danger ratings and fire bans", "Check cfs.sa.gov.au for today's fire danger rating until it is back."),
    "telecom": ("the phone network outage monitor", "Phone and internet outage alerts may not arrive until it is back."),
}
FEED_DOWN_AFTER = timedelta(minutes=60)


def source_alerts(health, loc, state, now, first_run):
    """A quiet 'System check' note when a feed the family relies on has been unreadable for an hour, and one
    more when it returns. Short blips say nothing."""
    sm = (loc or {}).get("summary") or {}
    ok = {"cfs": bool(sm.get("cfs_feed_ok")), "fdr": bool(sm.get("fire_danger_ok")),
          "telecom": bool((health.get("telecom") or {}).get("ok"))}
    down = state.setdefault("feed_down", {})
    out = []

    def note(key, title, message, tag):
        if not first_run:
            out.append({"key": key, "channels": ["proactive"], "category": "system", "title": title, "message": message,
                        "priority": 2, "confirmed": True, "tags": [tag]})

    for key, (name, advice) in CRITICAL_FEEDS.items():
        rec = down.get(key)
        if ok[key]:
            if rec and rec.get("alerted"):
                note(f"feed-up-{key}", f"System check: {name} working again"[:90],
                     f"Early Warning can read {name} again. Alerts that depend on it are back to normal.", "white_check_mark")
            down.pop(key, None)
            continue
        rec = down.setdefault(key, {"since": iso(now)})
        since = datetime.strptime(rec["since"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        if not rec.get("alerted") and now - since >= FEED_DOWN_AFTER:
            rec["alerted"] = iso(now)
            note(f"feed-down-{key}", f"System check: {name} not updating"[:90],
                 f"Early Warning has not been able to read {name} for about an hour, so it cannot warn you from that source. {advice}",
                 "wrench")
    return out


def local_alerts(loc, state, now, first_run):
    """Bushfire emergency warnings, extreme fire danger near Adelaide, and severe grid shortfalls in SA."""
    sm = (loc or {}).get("summary") or {}
    cd = state.setdefault("cooldown", {})
    day = now.strftime("%Y-%m-%d")
    out = []

    def add(key, hours, title, message, priority, tag):
        last = cd.get(key)
        if last and now - datetime.strptime(last, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc) < timedelta(hours=hours):
            return
        cd[key] = iso(now)
        if not first_run:
            out.append({"key": key, "channels": ["proactive"], "category": "nature", "title": title[:90],
                        "message": message[:390], "priority": priority, "confirmed": True, "tags": [tag]})

    for w in sm.get("emergency_warnings", [])[:3]:
        add("cfs-ew-" + "".join(ch for ch in w.lower() if ch.isalnum())[:40], 12, "CFS Emergency Warning",
            f"{w}. Check alert.sa.gov.au and ABC Radio Adelaide 891 AM and follow CFS advice now.", 5, "fire")
    for d, r in (sm.get("worst_fire_danger") or {}).items():
        if r in CONFIG["local"]["fdr_alert"]:
            add(f"fdr-{d}-{day}-{r}", 20, f"{r} fire danger: {d}",
                f"{r} fire danger rating for {d}. Review your bushfire plan, avoid high-risk areas and keep "
                "phones charged. Check cfs.sa.gov.au for total fire bans.", 4, "fire")
    if sm.get("possible_blackout"):
        add(f"blackout-{now:%Y%m%d%H}", 3, "UNCONFIRMED: possible large power outage in SA",
            f"SA electricity demand fell {sm.get('grid_demand_fall_pct')}% in under an hour to {sm.get('grid_demand_mw')} MW "
            f"(usual ~{sm.get('grid_demand_usual_mw') or '?'} MW). A sudden fall like this can mean a large blackout. "
            "Charge phones now if you still have power; check SA Power Networks and ABC 891.", 4, "zap")
    lvl = sm.get("grid_lor_level") or 0
    if lvl >= 2:
        kind = "Actual" if sm.get("grid_lor_actual") else "Forecast"
        add(f"lor-{day}-{lvl}-{kind}", 12, f"Power supply tight in SA ({kind} LOR{lvl})",
            f"AEMO has issued a {kind.lower()} lack of reserve level {lvl} notice for SA. "
            + ("Rolling blackouts are possible; charge phones and power banks, and plan for medical devices."
               if lvl >= 3 else "Supply is tight; blackouts are unlikely but possible if more generators fail."),
            4 if lvl >= 3 or kind == "Actual" else 3, "zap")
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
    loc, chk, pm = {}, {}, {}
    for name, fn in (("local_sa", lambda: localsa.fetch_local(state)), ("chokepoints", foresight.fetch_chokepoints),
                     ("prediction_markets", lambda: foresight.fetch_markets(state, now))):
        t0 = time.time()
        try:
            val = fn()
            health[name] = {"ok": True, "secs": round(time.time() - t0, 1)}
            if isinstance(val, dict) and val.get("errors"):
                health[name]["partial_errors"] = val["errors"][:5]
        except Exception as e:  # noqa: BLE001
            val = {}
            health[name] = {"ok": False, "error": f"{type(e).__name__}: {e}"[:300]}
        if name == "local_sa":
            loc = val
        elif name == "chokepoints":
            chk = val
        else:
            pm = val
    if loc.get("summary") is not None:
        nw = (results.get("news_watch") or {}).get("watch") or {}
        pick = lambda k, words: [headline(i)[:140] for i in (nw.get(k) or {}).get("items", [])
                                 if re.search(words, headline(i), re.I)][:3]
        loc["summary"]["sawater_news"] = pick("sawater", r"SA Water")
        loc["summary"]["power_news"] = power_reports((nw.get("sapn") or {}).get("items"))
        loc["summary"]["murray_news"] = pick("murray", r"River Murray")
    if loc.get("samples"):
        save_json("data/debug_samples.json", loc.pop("samples"))
    alerts += local_alerts(loc, state, now, first_run)
    alerts += source_alerts(health, loc, state, now, first_run)
    alerts += cable_alerts(mar, state, now, first_run, (results.get("telecom") or {}).get("networks"),
                           (results.get("news_watch") or {}).get("watch"))
    if loc.get("summary") is not None:
        loc["summary"]["cable_watch"] = [f"{w['flag']} vessel {w['name']} ({w['zone']})" for w in state.get("cable_watch", {}).values()]
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
        "local": loc.get("summary") or {},
        "chokepoints": chk,
        "pmarkets": (pm or {}).get("markets") or [],
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
        if not args.dry_run:
            queue_retry(a, now)
    try:
        log += relay_outbox(now, args.dry_run)
    except Exception as e:  # noqa: BLE001 - never lose this check's data or its "already alerted" record
        print(f"outbox relay failed: {type(e).__name__}: {e}")
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
    summary["local_sa"] = loc
    summary["chokepoints"] = {k: {kk: vv for kk, vv in v.items() if kk != "daily"} for k, v in chk.items()}
    summary["prediction_markets"] = (pm or {}).get("markets")
    if chk:
        save_json("data/chokepoints.json", {"generated_at": iso(now), "chokepoints": chk})
    save_json("data/latest.json", summary)
    save_json("state/state.json", state)

    ok = sum(1 for h in health.values() if h["ok"])
    print(f"{iso(now)}: {ok}/{len(health)} sources ok, {len(log)} alerts{' (bootstrap run)' if first_run else ''}")
    for name, h in health.items():
        if not h["ok"]:
            print(f"  FAILED {name}: {h['error']}")


if __name__ == "__main__":
    main()
