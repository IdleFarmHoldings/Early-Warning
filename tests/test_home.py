import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "collector"))
import home  # noqa: E402
from common import iso  # noqa: E402

# Sunday 15 Nov 2026, 5:00 pm in Adelaide (UTC+10:30)
EVENING = datetime(2026, 11, 15, 6, 30, tzinfo=timezone.utc)


def test_outlook_parsers_and_day_before_alerts():
    d = {"daily": {"time": ["2026-11-15", "2026-11-16", "2026-11-17"], "temperature_2m_max": [28.4, 41.2, 30.0],
                   "wind_gusts_10m_max": [30.1, 72.0, 20.0], "precipitation_sum": [0, 0, 1.2]}}
    days = home.parse_daily(d)
    assert days[1] == {"date": "2026-11-16", "tmax": 41, "gust": 72, "rain": 0}
    air = home.parse_air({"current": {"pm2_5": 12.3, "us_aqi": 40}, "hourly": {"pm2_5": [10, None, 64.2]}})
    assert air == {"pm25": 12, "aqi": 40, "peak_pm25_48h": 64} and home.air_word(12) == "Good" and home.air_word(120) == "Very poor"
    state = {}
    out = home.outlook_alerts({"days": days, "air": air}, state, EVENING, False)
    assert [a["title"] for a in out] == ["Tomorrow: hot and windy"] and out[0]["priority"] == 4 and "41 C" in out[0]["message"]
    assert home.outlook_alerts({"days": days, "air": air}, state, EVENING + timedelta(hours=1), False) == []  # once
    # morning: no day-before note yet; hot but calm -> heat note; smoke now -> smoke note
    assert home.outlook_alerts({"days": days}, {}, EVENING - timedelta(hours=9), False) == []
    calm = [dict(days[0]), {**days[1], "gust": 25}]
    out = home.outlook_alerts({"days": calm, "air": {"pm25": 130}}, {}, EVENING, False)
    assert [a["title"] for a in out] == ["Tomorrow: very hot (41 C)", "Smoke in the air near home (very poor)"]
    assert all(len(a["message"]) < 390 for a in out)


def test_bureau_warnings_only_for_our_area():
    loc = {"bom": [{"title": "Severe Thunderstorm Warning for North West Pastoral forecast district", "severe": True},
                   {"title": "Strong Wind Warning for coastal waters between WA-SA Border and Fowlers Bay", "severe": False},
                   {"title": "Cancellation of Severe Weather Warning for Mount Lofty Ranges", "severe": True},
                   {"title": "Severe Weather Warning for damaging winds for Mount Lofty Ranges and Mid North forecast districts", "severe": True},
                   {"title": "Heatwave Warning for South Australia", "severe": True}]}
    state = {}
    out = home.bom_alerts(loc, state, EVENING, False)
    assert len(out) == 2 and "Mount Lofty Ranges" in out[0]["message"] and "Heatwave" in out[1]["message"]
    assert home.bom_alerts(loc, state, EVENING + timedelta(hours=2), False) == []


def test_fuel_price_pressure_and_shortage_reports():
    series = []
    for h in range(0, 9 * 24, 2):
        t = EVENING - timedelta(hours=9 * 24 - h)
        brent = 95 if h < 8 * 24 else 110  # a jump in the last day
        series.append({"ts": iso(t), "mkt_Brent oil": brent, "mkt_AUD/USD": 0.68})
    fo = home.fuel_outlook(series)
    assert fo["change_pct"] == 15.8 and fo["pump_cents"] == 14
    news = {"fuel_au": {"items": [
        {"title": "Servos run dry across Adelaide as panic buying spreads - ABC News"},
        {"title": "Petrol shortage: SA motorists queue for fuel - Adelaide Now"},
        {"title": "Diesel rationing begins at regional South Australia service stations - InDaily"},
        {"title": "Fuel shortage fears ease in Australia as tankers arrive - SMH"},
        {"title": "Pakistan fuel shortage worsens - Dawn"}]}}
    state = {}
    out, _ = home.fuel_alerts(series, news, state, EVENING, False)
    assert [a["title"] for a in out] == ["Fuel prices likely to rise", "Fuel shortage reports in Australia"]
    assert "14 cents" in out[0]["message"] and out[1]["priority"] == 4
    out, _ = home.fuel_alerts(series, news, state, EVENING + timedelta(hours=3), False)
    assert out == []
    quiet, fo = home.fuel_alerts(series[:80], {"fuel_au": {"items": news["fuel_au"]["items"][3:]}}, {}, EVENING, False)
    assert quiet == []


def test_weekly_ok_drill_and_repeats():
    sunday_morning = datetime(2026, 11, 14, 23, 0, tzinfo=timezone.utc)  # Sunday 9:30 am Adelaide
    state = {}
    out = home.weekly_ok({"a": {"ok": True}, "acsc": {"ok": False}}, state, sunday_morning, False, 6)
    assert len(out) == 1 and "1 of 2 data feeds" in out[0]["message"] and out[0]["priority"] == 2
    assert home.weekly_ok({}, state, sunday_morning + timedelta(minutes=30), False, 6) == []
    assert home.weekly_ok({}, {}, EVENING, False, 0) == []
    drill_day = datetime(2027, 1, 1, 23, 45, tzinfo=timezone.utc)  # Saturday 2 Jan 2027, 10:15 am Adelaide
    out = home.quarterly_drill({}, drill_day, False)
    assert len(out) == 1 and out[0]["priority"] == 5 and out[0]["channels"] == ["proactive", "reactive"]
    assert home.quarterly_drill({}, EVENING, False) == []
    files = home.nag_files([{"key": "cfs-ew-x", "title": "CFS Emergency Warning", "message": "m", "priority": 5,
                             "channels": ["proactive"], "tags": ["fire"]},
                            {"key": "x", "title": "t", "message": "m", "priority": 4, "channels": ["proactive"]},
                            *out], drill_day)
    assert len(files) == 2 and files[0][1]["title"] == "REPEAT 2: CFS Emergency Warning" and files[0][1]["priority"] == 5
    assert files[1][1]["not_before"] == iso(drill_day + timedelta(minutes=9))


def test_relay_holds_a_repeat_until_it_is_due(tmp_path, monkeypatch):
    import run
    monkeypatch.setattr(run, "send", lambda a, dry: {c: True for c in a["channels"]})
    now = EVENING
    p = tmp_path / "x-repeat2.json"
    p.write_text(json.dumps({"channel": "proactive", "title": "REPEAT 2: t", "message": "m", "priority": 5,
                             "not_before": iso(now + timedelta(minutes=4))}))
    assert run._relay_one(p, tmp_path, now, False) is None and p.exists()
    done = run._relay_one(p, tmp_path, now + timedelta(minutes=5), False)
    assert done and done["priority"] == 5 and not p.exists()
