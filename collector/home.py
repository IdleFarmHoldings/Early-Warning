"""Things that matter at home: weather and smoke outlook, local Bureau warnings, fuel, and system self-checks.

Pure functions apart from fetch_outlook(), so they can be tested offline. All alerts use the same dict shape as
run.local_alerts().
"""
import re
from datetime import datetime, timedelta, timezone
from statistics import median
from zoneinfo import ZoneInfo

from common import CONFIG, get_json, iso

HC = CONFIG["home"]
ADL = ZoneInfo("Australia/Adelaide")


def _ts(s):
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


class Alerts:
    """Collects alerts with a per-key cooldown held in state['cooldown']."""

    def __init__(self, state, now, first_run):
        self.cd, self.now, self.first, self.out = state.setdefault("cooldown", {}), now, first_run, []

    def add(self, key, hours, title, message, priority, tag, category="nature", channels=("proactive",), confirmed=True):
        last = self.cd.get(key)
        if last and self.now - _ts(last) < timedelta(hours=hours):
            return False
        self.cd[key] = iso(self.now)
        if not self.first:
            self.out.append({"key": key, "channels": list(channels), "category": category, "title": title[:90],
                             "message": message[:390], "priority": priority, "confirmed": confirmed, "tags": [tag]})
        return True


# ---------- Weather and smoke outlook (Open-Meteo: free, no key; a forecast model, not a local sensor) ----------
def fetch_outlook():
    """Next few days' heat, wind and rain plus modelled smoke (PM2.5) near home.
    The location is rounded to about 10 km before it is sent."""
    h = CONFIG["local"]["home"]
    lat, lon = round(h["lat"], 1), round(h["lon"], 1)
    out, errors = {}, []
    try:
        d = get_json("https://api.open-meteo.com/v1/forecast?"
                     f"latitude={lat}&longitude={lon}&daily=temperature_2m_max,wind_gusts_10m_max,precipitation_sum"
                     "&forecast_days=4&timezone=Australia%2FAdelaide", timeout=20)
        out["days"] = parse_daily(d)
    except Exception as e:  # noqa: BLE001
        errors.append(f"forecast: {type(e).__name__}: {e}"[:160])
    try:
        d = get_json("https://air-quality-api.open-meteo.com/v1/air-quality?"
                     f"latitude={lat}&longitude={lon}&current=pm2_5,us_aqi&hourly=pm2_5&forecast_days=2"
                     "&timezone=Australia%2FAdelaide", timeout=20)
        out["air"] = parse_air(d)
    except Exception as e:  # noqa: BLE001
        errors.append(f"air: {type(e).__name__}: {e}"[:160])
    if not out:
        raise RuntimeError("; ".join(errors))
    out["errors"] = errors
    return out


def parse_daily(d):
    dy = d.get("daily") or {}
    days = []
    for i, date in enumerate(dy.get("time") or []):
        def val(k):
            v = (dy.get(k) or [None] * (i + 1))[i]
            return round(v) if isinstance(v, (int, float)) else None
        days.append({"date": date, "tmax": val("temperature_2m_max"), "gust": val("wind_gusts_10m_max"),
                     "rain": val("precipitation_sum")})
    return days


def parse_air(d):
    cur = d.get("current") or {}
    hourly = [v for v in ((d.get("hourly") or {}).get("pm2_5") or []) if isinstance(v, (int, float))]
    pm = cur.get("pm2_5")
    return {"pm25": round(pm) if isinstance(pm, (int, float)) else None, "aqi": cur.get("us_aqi"),
            "peak_pm25_48h": round(max(hourly)) if hourly else None}


def air_word(pm):
    if pm is None:
        return None
    return "Good" if pm < 25 else "Fair" if pm < 50 else "Poor" if pm < 100 else "Very poor" if pm < 300 else "Hazardous"


def outlook_alerts(outlook, state, now, first_run):
    """Day-ahead heat and wind, and smoke in the air now."""
    a = Alerts(state, now, first_run)
    local = now.astimezone(ADL)
    today = local.strftime("%Y-%m-%d")
    days = {d["date"]: d for d in (outlook or {}).get("days") or []}
    tomorrow = (local + timedelta(days=1)).strftime("%Y-%m-%d")
    # Day-before notes go out in the afternoon and evening, when there is still time to get ready.
    t = days.get(tomorrow)
    if t and 14 <= local.hour <= 21:
        hot, gust = t.get("tmax") or 0, t.get("gust") or 0
        if hot >= HC["fire_weather_temp_c"] and gust >= HC["fire_weather_gust_kmh"]:
            a.add(f"wx-fire-{tomorrow}", 30, "Tomorrow: hot and windy",
                  f"Forecast near home tomorrow: {hot} C with gusts to {gust} km/h. That is bad fire weather. Tonight: check "
                  "the CFS fire danger rating and bans, charge phones, fuel the car, and agree where you'd go and when.",
                  4, "fire")
        elif hot >= HC["heat_alert_c"]:
            a.add(f"wx-heat-{tomorrow}", 30, f"Tomorrow: very hot ({hot} C)",
                  f"Forecast near home tomorrow: {hot} C. Plan to stay cool, check on older family and neighbours, look "
                  "after pets, and expect power demand to be high.", 4 if hot >= HC["heat_severe_c"] else 3, "thermometer")
        elif gust >= HC["wind_alert_kmh"]:
            a.add(f"wx-wind-{tomorrow}", 30, f"Tomorrow: damaging winds (gusts {gust} km/h)",
                  f"Forecast gusts near home tomorrow reach {gust} km/h. Tie down loose items, park away from trees and "
                  "charge phones in case power lines come down.", 3, "wind_face")
    pm = ((outlook or {}).get("air") or {}).get("pm25")
    if pm is not None and pm >= HC["smoke_alert_pm25"]:
        bad = pm >= HC["smoke_severe_pm25"]
        a.add(f"air-{today}-{'bad' if bad else 'poor'}", 12, f"Smoke in the air near home ({air_word(pm).lower()})",
              f"Fine particles are about {pm} micrograms per cubic metre near home (under 25 is good). This is a forecast "
              "model, not a local sensor. Check the CFS map for fires upwind; keep windows closed if you can smell smoke.",
              4 if bad else 3, "fog")
    return a.out


# ---------- Bureau warnings that actually cover home ----------
def bom_alerts(loc, state, now, first_run):
    """Severe Bureau warnings for our forecast districts (or statewide ones). Warnings that name only other
    districts - the far north, the west coast - are left on the board."""
    a = Alerts(state, now, first_run)
    mine = re.compile("|".join(re.escape(d) for d in HC["bom_districts"]), re.I)
    other = re.compile(r"forecast districts?|coastal waters|\bwaters\b|Gulf|Peninsula|Pastoral|Coast\b|Island|Riverland|"
                       r"Murraylands|Mid North|Flinders|South East", re.I)
    for w in (loc or {}).get("bom") or []:
        title = w.get("title") or ""
        if not w.get("severe") or re.search(r"^cancellation|sheep graziers|marine|surf", title, re.I):
            continue
        if not mine.search(title) and other.search(title):
            continue
        key = "bom-" + "".join(ch for ch in title.lower() if ch.isalnum())[:60]
        a.add(key, 18, "Bureau warning for our area", f"{title}. Check bom.gov.au/sa/warnings for the detail and timing, "
              "and secure anything loose outside.", 4, "cloud_with_lightning")
    return a.out


# ---------- Fuel: oil price pressure and shortage reports ----------
def fuel_outlook(series):
    """How far oil has moved in Australian dollars over a week, and what that usually does to pump prices.
    Crude feeds through to Adelaide bowsers over one to two weeks; one US barrel is 159 litres."""
    pts = []
    for r in series or []:
        b, fx = r.get("mkt_Brent oil"), r.get("mkt_AUD/USD")
        if isinstance(b, (int, float)) and isinstance(fx, (int, float)) and fx > 0:
            pts.append((_ts(r["ts"]), b / fx))
    if len(pts) < 20:
        return None
    last_t = pts[-1][0]
    recent = [v for t, v in pts if last_t - t <= timedelta(hours=12)]
    past = [v for t, v in pts if timedelta(days=6) <= last_t - t <= timedelta(days=8)]
    if not recent or len(past) < 5:
        return None
    now_aud, then_aud = median(recent), median(past)
    return {"brent_aud": round(now_aud, 1), "week_ago_aud": round(then_aud, 1),
            "change_pct": round((now_aud - then_aud) / then_aud * 100, 1),
            "pump_cents": round((now_aud - then_aud) / 159 * 100)}


SHORT = re.compile(r"\b(shortages?|run(s|ning)? (dry|out)|ran (dry|out)|out of (fuel|petrol|diesel)|rationing|rationed|"
                   r"panic[- ]buying|queues?|limits? (on )?(fuel|purchases)|no (fuel|petrol|diesel))\b", re.I)
FUEL = re.compile(r"\b(fuel|petrol|diesel|servos?|service stations?|bowsers?)\b", re.I)
AU = re.compile(r"\b(Australia\w*|Adelaide|South Australia\w*|SA\b|Sydney|Melbourne|Brisbane|Perth|NSW|Victoria\w*|Queensland\w*|"
                r"nationwide|Aussie)\b")
OLD = re.compile(r"\b(years? ago|last year|anniversary|in 20(1\d|2[0-5])|eases?|eased|easing|ends?|ended|over\b|lifted|avoid(s|ed)?)\b", re.I)


def fuel_alerts(series, news, state, now, first_run):
    a = Alerts(state, now, first_run)
    fo = fuel_outlook(series)
    if fo and fo["change_pct"] >= HC["fuel_jump_pct"]:
        a.add("fuel-price", 72, "Fuel prices likely to rise",
              f"Oil is up {fo['change_pct']}% this week in Australian dollars. That usually adds about {fo['pump_cents']} "
              "cents a litre at Adelaide pumps within one to two weeks. Fill the car and any jerry cans before it flows through.",
              3, "fuelpump", category="supply")
    heads, pubs = [], set()
    for it in ((news or {}).get("fuel_au") or {}).get("items", []):
        t = it.get("title", "")
        head, pub = (t.rsplit(" - ", 1) + [""])[:2]
        if FUEL.search(head) and SHORT.search(head) and AU.search(head) and not OLD.search(head):
            heads.append(head)
            pubs.add(pub.strip().lower())
    if len(pubs) >= HC["fuel_shortage_outlets"]:
        a.add("fuel-shortage", 24, "Fuel shortage reports in Australia",
              f"{heads[0][:150]}. {len(pubs)} outlets are reporting shortages or panic buying. Top up now if you are under "
              "half a tank; do not hoard more than you need.", 4, "fuelpump", category="supply")
    return a.out, fo


# ---------- System self-checks ----------
def weekly_ok(health, state, now, first_run, alerts_7d):
    """A quiet note every Sunday morning so silence is never mistaken for safety."""
    local = now.astimezone(ADL)
    if not (local.weekday() == 6 and 9 <= local.hour < 12):
        return []
    a = Alerts(state, now, first_run)
    bad = sorted(k for k, v in (health or {}).items() if not v.get("ok"))
    feeds = f"{len(health) - len(bad)} of {len(health)} data feeds working" + (f" (down: {', '.join(bad[:4])})" if bad else "")
    a.add("weekly-ok", 100, "System OK: weekly check",
          f"Early Warning is running. {feeds}. {alerts_7d} alert{'s' if alerts_7d != 1 else ''} sent in the last 7 days. "
          "If you ever stop getting this on Sunday mornings, the system has stopped.", 2, "white_check_mark", category="system")
    return a.out


def quarterly_drill(state, now, first_run):
    """A loud test on the first Saturday of January, April, July and October at 10 am, on both channels."""
    local = now.astimezone(ADL)
    if not (local.month in (1, 4, 7, 10) and local.weekday() == 5 and local.day <= 7 and 10 <= local.hour < 12):
        return []
    a = Alerts(state, now, first_run)
    a.add("drill", 24 * 60, "DRILL - test of the emergency alert",
          "THIS IS A TEST. NOTHING HAS HAPPENED. This is how a real emergency alert arrives. If it did not make a sound "
          "or arrived silently, open the board and follow 'Make emergency alerts loud'. Take 2 minutes to check your plan.",
          5, "loudspeaker", category="system", channels=("proactive", "reactive"))
    return a.out


# ---------- Emergency alerts repeat so they are not slept through ----------
def nag_files(alerts, now):
    """For every priority-5 alert, extra outbox entries that re-send it a few minutes apart.
    Returns [(filename, payload)]; the relay delivers each one once its not_before time has passed."""
    out = []
    for al in alerts:
        if al.get("priority", 3) < 5 or al.get("category") == "system":
            continue
        ch = al.get("channels") or ["proactive"]
        for n, mins in enumerate(HC["repeat_minutes"], start=2):
            stem = re.sub(r"[^a-z0-9]+", "-", al["key"].lower()).strip("-")[:40]
            out.append((f"{now:%Y%m%dT%H%MZ}-{stem}-repeat{n}.json", {
                "channel": "both" if len(ch) == 2 else ch[0], "title": f"REPEAT {n}: {al['title']}"[:90],
                "message": al["message"], "priority": 5, "tags": al.get("tags") or ["rotating_light"],
                "source": "collector", "not_before": iso(now + timedelta(minutes=mins))}))
    return out
