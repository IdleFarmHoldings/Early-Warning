"""National shocks: a whole country's flights, power, phone/internet or payments halted at once.

Example: 13 Nov 2026, the FAA grounded every US flight "due to extraordinary circumstances". An unexplained
nationwide halt in an ally or rival is often the first visible sign of a cyber attack or security emergency.

Three independent signals, each failing in a different way:
  official  - FAA airport status feed (US aviation only): a national ground stop, or many big airports stopped at once.
              Ordinary weather / single-airline ground stops at a few airports happen daily and never count.
  traffic   - airborne aircraft around a country's big airports (adsb.fi), versus normal for that hour, two runs
              in a row, on at least half the country's hubs. If 3+ countries drop together it is a tracking problem,
              not a shock, and is ignored unless another signal agrees.
  news      - headlines from 3+ different outlets within a few hours that say it is nationwide / all flights.
              Single-airline, single-airport and "ground stop lifted" stories are filtered out.

Severity:
  Confirmed - 2+ signals agree, or the official feed alone   -> Early warning channel (Australia: both channels)
  Possible  - one signal, still there on the next run         -> UNCONFIRMED note on the Early warning channel
  An "appears to be over" note follows any alert once every signal has cleared.

Pure functions apart from the `state` dict, so they can be tested offline.
"""
import math
import re
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

from common import CONFIG, iso

CFG = CONFIG.get("shocks", {})

SECTORS = {
    "aviation": {
        "label": "flights",
        "halt": r"ground stops?|grounds? (all |every )?(\w+ )?(flights|planes|aircraft|air traffic)|"
                r"(flights?|planes|air traffic) (are |were |have been |has been )?(grounded|halted|suspended|stopped)|"
                r"halts? (all )?(\w+ )?(flights|air traffic)|"
                r"air traffic control (outage|failure|fault|system|problems?|issues?)|"
                r"airspace (is |was |has been )?(closed|closure|shut)|clos(e|es|ed|ing) (its |the )?airspace",
        "headline": "flights grounded nationwide",
    },
    "power": {
        "label": "power",
        "halt": r"(nationwide|national|countrywide|country-wide) (blackout|power (outage|cut|failure))|"
                r"(national )?grid collapse|plunged into darkness|blackout (across|hits) (the )?(country|nation)",
        "headline": "nationwide power outage",
    },
    "networks": {
        "label": "phone and internet",
        "halt": r"(nationwide|national|countrywide) (internet|mobile|phone|telecoms?|network) (outage|blackout|shutdown)|"
                r"internet (blackout|shutdown) (across|hits)",
        "headline": "nationwide phone/internet outage",
    },
    "payments": {
        "label": "payments",
        "halt": r"(nationwide|national) (payments?|card|banking|EFTPOS) (outage|failure|system failure)|"
                r"payment systems? (down|fail\w*) (nationwide|across the country)",
        "headline": "nationwide payments outage",
    },
}
SCALE = re.compile(
    r"\b(nationwide|national|countrywide|country-wide|across the (U\.?S\.?|US|United States|country|nation)|"
    r"all (U\.?S\.?|US|domestic|departing|commercial|civil|outbound) (flights|air traffic)|all flights|every flight|"
    r"entire (country|airspace|network)|until further notice|grid collapse|plunged into darkness|"
    r"(closes|closed|closing) (its )?airspace|airspace (is |was )?closed)\b", re.I)
AIRLINE = re.compile(
    r"\b(JetBlue|United Airlines|Delta|American Airlines|Southwest|Alaska Airlines|Spirit|Frontier|Hawaiian|Allegiant|"
    r"Qantas|Virgin|Jetstar|Rex|Air New Zealand|Ryanair|easyJet|British Airways|Lufthansa|Emirates|Qatar Airways|"
    r"Singapore Airlines|Cathay|Air India|IndiGo)\b", re.I)
AIRLINE_OK = re.compile(r"\b(FAA|air traffic|nationwide|national|airspace|all (U\.?S\.?|US) flights)\b", re.I)
RECOVERY = re.compile(r"\b(lift(s|ed|ing)?|resum(e|es|ed|ing)|reopen(s|ed|ing)?|restored|ends?|ended|back to normal)\b", re.I)
NOT_LIVE = re.compile(r"\b(anniversary|years? (ago|after)|lessons|inquiry|report finds|how to|explainer: what we learned|"
                      r"remember(ing)?|documentary|when .{3,40} closed, )\b", re.I)


def _country_patterns():
    out = {}
    for code, c in CFG.get("countries", {}).items():
        parts = []
        for w in c.get("words", []):
            esc = re.escape(w)
            # all-caps short words (US, UK, NZ, FAA) must match exactly so "us" or "uk" in prose do not count
            parts.append(f"(?-i:(?<![A-Za-z]){esc}(?![A-Za-z]))" if (w.isupper() and len(w) <= 4) or "." in w
                         else f"(?<![A-Za-z]){esc}(?![A-Za-z])")
        out[code] = re.compile("|".join(parts), re.I) if parts else None
    return out


COUNTRY_RX = _country_patterns()


def _when(item):
    try:
        dt = parsedate_to_datetime(item.get("date") or "")
        return dt if dt and dt.tzinfo else None
    except (TypeError, ValueError):
        return None


def _split(title):
    return (title.rsplit(" - ", 1) + [""])[:2] if " - " in title else [title, ""]


def _ts(s):
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


# ---------- official: FAA airport status ----------
def parse_faa(raw):
    """FAA NAS status (XML). Returns ground stops and whether a national ground stop is in force.
    Defensive: the feed layout is not formally documented, so anything unreadable gives ok=False."""
    text = raw.decode("utf-8", "replace") if isinstance(raw, (bytes, bytearray)) else str(raw or "")
    if "<" not in text:
        return {"ok": False, "ground_stops": [], "national": False, "airports": 0}
    stops = []
    # Any delay section whose name mentions a ground stop, whatever its inner tag names are
    blocks = re.findall(r"<Ground_Stop_List>(.*?)</Ground_Stop_List>", text, re.S | re.I)
    blocks += [b for b in re.findall(r"<Delay_type>(.*?)</Delay_type>", text, re.S | re.I)
               if re.search(r"<Name>[^<]*ground stop", b, re.I) and "<Ground_Stop_List>" not in b]
    for block in blocks:
        progs = re.findall(r"<(?:Program|Ground_Stop)>(.*?)</(?:Program|Ground_Stop)>", block, re.S | re.I) or [block]
        for prog in progs:
            g = lambda tag: (re.search(f"<{tag}>(.*?)</{tag}>", prog, re.S | re.I) or [None, ""])[1].strip()
            for arpt in re.findall(r"<ARPT>(.*?)</ARPT>", prog, re.S | re.I) or [""]:
                stops.append({"airport": arpt.strip(), "reason": g("Reason")[:120], "end": g("End_Time")[:40]})
    stops = [s for s in stops if s["airport"] or s["reason"]]
    national_rx = re.compile(r"\b(nationwide|national ground stop|all airports|all (US|U\.S\.) airports|"
                             r"NAS[- ]wide|entire NAS)\b", re.I)
    national = bool(national_rx.search(text))
    airports = len({s["airport"] for s in stops if s["airport"]})
    return {"ok": "AIRPORT_STATUS_INFORMATION" in text.upper() or bool(stops), "ground_stops": stops[:40],
            "national": national, "airports": airports,
            "updated": ((re.search(r"<Update_Time>(.*?)</Update_Time>", text, re.S | re.I) or [None, ""])[1]).strip()}


def official_signal(faa):
    """(on, detail) for the US aviation official signal."""
    if not faa or not faa.get("ok"):
        return False, None
    if faa.get("national"):
        return True, "FAA airport status shows a national ground stop"
    n = faa.get("airports", 0)
    if n >= CFG.get("faa_min_airports", 15):
        return True, f"FAA airport status: ground stops at {n} airports at once"
    return False, None


# ---------- traffic: airborne aircraft near each country's hubs ----------
def hub_slug(name):
    return "trf_" + re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def traffic_signals(current, series, now, baseline_fn):
    """Per country: hubs with a baseline, hubs well below normal, and the ratios. `current` is {hub name: count}."""
    t = CFG.get("traffic", {})
    out = {}
    for code, c in CFG.get("countries", {}).items():
        hubs = c.get("hubs") or []
        if not hubs:
            continue
        rows, down = [], []
        for h in hubs:
            v = (current or {}).get(h)
            if v is None:
                continue
            b = baseline_fn(series, hub_slug(h), now)
            if not b or b["median"] < t.get("min_median", 30):
                continue
            ratio = v / b["median"]
            z = (v - b["median"]) / b["spread"]
            rows.append({"hub": h, "value": v, "median": round(b["median"]), "ratio": round(ratio, 2), "z": round(z, 1)})
            if ratio <= t.get("max_ratio", 0.6) and z <= -t.get("z", 3.0):
                down.append(h)
        if not rows:
            out[code] = {"status": "learning baseline", "hubs": []}
            continue
        need = max(1, math.ceil(len(rows) * t.get("min_hub_share", 0.5)))
        out[code] = {"status": "DOWN" if len(down) >= need else "normal", "hubs": rows, "down": down,
                     "ratio": round(sum(r["ratio"] for r in rows) / len(rows), 2)}
    return out


# ---------- news ----------
def news_signals(news, now):
    """{(country, sector): {"outlets": set, "headlines": [...], "recovery": set, "last": dt}} from recent headlines."""
    hours = CFG.get("news_window_hours", 3)
    out = {}
    seen = set()
    for key in CFG.get("news_keys", []):
        for it in ((news or {}).get(key) or {}).get("items", []):
            head, pub = _split(it.get("title", ""))
            dt = _when(it)
            if not dt or now - dt > timedelta(hours=hours) or dt - now > timedelta(hours=1):
                continue
            norm = re.sub(r"\W+", " ", head.lower()).strip()
            if norm in seen or NOT_LIVE.search(head):
                continue
            seen.add(norm)
            for sector, s in SECTORS.items():
                if not re.search(s["halt"], head, re.I):
                    continue
                recovery = bool(RECOVERY.search(head))
                if not recovery and not SCALE.search(head):
                    continue
                if sector == "aviation" and AIRLINE.search(head) and not AIRLINE_OK.search(head):
                    continue  # one airline's own ground stop
                for code, rx in COUNTRY_RX.items():
                    if rx and rx.search(head):
                        c = out.setdefault((code, sector), {"outlets": set(), "headlines": [], "recovery": set(),
                                                            "last_halt": None, "last_recovery": None})
                        p = (pub or "unknown").strip().lower()
                        if recovery:
                            c["recovery"].add(p)
                            c["last_recovery"] = max(filter(None, [c["last_recovery"], dt]))
                        else:
                            c["outlets"].add(p)
                            c["last_halt"] = max(filter(None, [c["last_halt"], dt]))
                            c["headlines"].append({"title": head[:160], "pub": pub.strip(), "at": iso(dt),
                                                   "link": it.get("link", "")})
    return out


def news_on(n):
    """Enough outlets, and not overtaken by 'lifted / resumed' reports."""
    if not n or len(n["outlets"]) < CFG.get("news_min_outlets", 3):
        return False
    if len(n["recovery"]) >= 2 and n["last_recovery"] and n["last_halt"] and n["last_recovery"] > n["last_halt"]:
        return False
    return True


# ---------- combine ----------
RANK = {"none": 0, "Possible": 1, "Confirmed": 2}


def evaluate(results, series, now, state, first_run, baseline_fn):
    """Returns (summary for latest.json, alerts)."""
    mem = state.setdefault("shocks", {})
    cd = state.setdefault("cooldown", {})
    faa = (results or {}).get("faa") or {}
    off_on, off_detail = official_signal(faa)
    traffic = traffic_signals(((results or {}).get("aircraft") or {}).get("traffic_by_hub") or {}, series, now, baseline_fn)
    news = news_signals(((results or {}).get("news_watch") or {}).get("watch"), now)

    down_now = sorted(c for c, v in traffic.items() if v.get("status") == "DOWN")
    tracking_glitch = len(down_now) >= CFG.get("traffic", {}).get("glitch_countries", 3)
    prev_down = mem.get("traffic_down", {})
    mem["traffic_down"] = {c: prev_down.get(c, iso(now)) for c in down_now}

    keys = set(news) | {(c, "aviation") for c in down_now} | ({("US", "aviation")} if off_on else set())
    keys |= {tuple(k.split("|")) for k in mem.get("alerted", {})}
    shocks, alerts = [], []
    names = {c: v.get("name", c) for c, v in CFG.get("countries", {}).items()}
    for code, sector in sorted(keys):
        sig = {}
        if sector == "aviation" and code == "US" and off_on:
            sig["official"] = off_detail
        tv = traffic.get(code) or {}
        if sector == "aviation" and code in down_now:
            first = mem["traffic_down"][code]
            sustained = now - _ts(first) >= timedelta(minutes=CFG.get("traffic", {}).get("sustain_minutes", 10))
            if sustained and not tracking_glitch:
                sig["traffic"] = (f"airborne traffic near {', '.join(tv['down'][:3])} about "
                                  f"{round(min(h['ratio'] for h in tv['hubs'] if h['hub'] in tv['down']) * 100)}% of normal")
        n = news.get((code, sector))
        if news_on(n):
            sig["news"] = f"{len(n['outlets'])} outlets: {n['headlines'][0]['title']}"
        # a tracking glitch can still count when another signal independently agrees
        if sector == "aviation" and code in down_now and tracking_glitch and sig:
            sig["traffic"] = f"airborne traffic near {', '.join(tv['down'][:3])} well below normal"
        level = "Confirmed" if len(sig) >= 2 or "official" in sig else "Possible" if sig else "none"
        # One airport area going quiet (drone closures around Moscow, a storm, a receiver outage) is not a nation:
        # traffic on its own may only alert where several hubs are measured.
        board_only = (level == "Possible" and list(sig) == ["traffic"]
                      and len(tv.get("hubs") or []) < CFG.get("traffic", {}).get("alone_min_hubs", 2))
        k = f"{code}|{sector}"
        m = mem.setdefault("seen", {})
        if level == "Possible":
            m.setdefault(k, iso(now))
        else:
            m.pop(k, None)
        if level != "none":
            shocks.append({"country": names.get(code, code), "code": code, "sector": sector, "level": level,
                           "signals": sig, "headlines": (n or {}).get("headlines", [])[:3]})
        if board_only:
            continue
        alerts += _alerts_for(code, sector, level, sig, mem, cd, now, first_run, names)
    return {"generated_at": iso(now), "shocks": shocks, "faa": {k: faa.get(k) for k in ("ok", "airports", "national", "updated")}
            | {"ground_stops": (faa.get("ground_stops") or [])[:8]},
            "traffic": traffic, "tracking_glitch": tracking_glitch}, alerts


def _alerts_for(code, sector, level, sig, mem, cd, now, first_run, names):
    k = f"{code}|{sector}"
    alerted = mem.setdefault("alerted", {})
    name, s = names.get(code, code), SECTORS[sector]
    au = code == "AU"
    out = []

    def emit(key, channels, title, message, priority, confirmed, tag):
        if not first_run:
            out.append({"key": key, "channels": channels, "category": "cyber", "title": title[:90],
                        "message": message[:390], "priority": priority, "confirmed": confirmed, "tags": [tag]})

    if level == "none":
        if k in alerted:
            cleared = mem.setdefault("clear_since", {}).setdefault(k, iso(now))
            if now - _ts(cleared) >= timedelta(minutes=CFG.get("clear_minutes", 30)):
                emit(f"shock-over-{code}-{sector}", ["proactive"], f"Update: {name} {s['label']} - appears to be over",
                     f"The signs of a nationwide {s['label']} halt in {name} have cleared (news, tracking and official feeds). "
                     "Check the board for what caused it.", 2, True, "white_check_mark")
                alerted.pop(k)
                mem["clear_since"].pop(k, None)
        return out
    mem.setdefault("clear_since", {}).pop(k, None)
    if level == "Possible":
        first = mem.get("seen", {}).get(k)
        if not first or now - _ts(first) < timedelta(minutes=CFG.get("possible_persist_minutes", 25)):
            return out  # wait for it to persist before pushing a single unconfirmed signal
    prev = alerted.get(k)
    if prev:
        at, lvl = prev.split("|")
        if RANK[level] <= RANK.get(lvl, 0) and now - _ts(at) < timedelta(hours=CFG.get("cooldown_hours", 12)):
            return out
    alerted[k] = f"{iso(now)}|{level}"
    cd[f"shock-{code}-{sector}"] = iso(now)
    why = "; ".join(sig.values())
    if level == "Confirmed":
        advice = ("Expect knock-on effects; keep phones charged and some cash on hand. Check the board."
                  if au else "Cause not yet known. Flights to and from there may be disrupted; check the board."
                  if sector == "aviation" else "Cause not yet known; watch for a link to Australia. Check the board.")
        emit(f"shock-{code}-{sector}", ["proactive", "reactive"] if au else ["proactive"],
             f"{name}: {s['headline']}", f"{why}. {advice}", 5 if au else 4, True, "airplane" if sector == "aviation" else "warning")
    else:
        emit(f"shock-{code}-{sector}", ["proactive"], f"UNCONFIRMED: possible {s['headline']} ({name})",
             f"One sign so far: {why}. Not yet confirmed by a second source; watching.", 4 if au else 3, False, "eyes")
    return out
