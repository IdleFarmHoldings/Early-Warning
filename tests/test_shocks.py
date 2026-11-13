"""National shocks: replay the 13 Nov 2026 US ground stop, and check everyday noise stays quiet."""
import sys
from datetime import timedelta
from email.utils import format_datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "collector"))

from common import iso, now_utc  # noqa: E402
from rules import baseline  # noqa: E402
import shocks as S  # noqa: E402

NOW = now_utc().replace(second=0, microsecond=0)
US_HUBS = S.CFG["countries"]["US"]["hubs"]
AU_HUBS = S.CFG["countries"]["AU"]["hubs"]
ALL_HUBS = sorted({h for c in S.CFG["countries"].values() for h in c.get("hubs", [])})


def item(title, mins_ago=20):
    return {"title": title, "date": format_datetime(NOW - timedelta(minutes=mins_ago)), "link": "https://x/" + title[:12]}


def news(*titles, key="aviation_halt", start=10):
    return {key: {"items": [item(t, start + 10 * i) for i, t in enumerate(titles)]}}


def history(normal=400):
    """10 days of 15-minute rows with every hub at `normal` airborne aircraft."""
    rows, t = [], NOW - timedelta(days=10)
    while t < NOW - timedelta(minutes=15):
        rows.append({"ts": iso(t), **{S.hub_slug(h): normal for h in ALL_HUBS}})
        t += timedelta(minutes=15)
    return rows


def traffic(factor=1.0, hubs=None, normal=400):
    return {h: int(normal * (factor if (hubs is None or h in hubs) else 1.0)) for h in ALL_HUBS}


FAA_NATIONAL = b"""<AIRPORT_STATUS_INFORMATION><Update_Time>Fri Nov 13 21:30:00 2026 GMT</Update_Time>
<Delay_type><Name>Ground Stop Programs</Name><Ground_Stop_List>
""" + b"".join(f"<Program><ARPT>{a}</ARPT><Reason>equipment / outage</Reason><End_Time>until further notice</End_Time></Program>".encode()
               for a in ["ATL", "ORD", "DFW", "DEN", "LAX", "JFK", "SFO", "SEA", "LAS", "MCO", "CLT", "PHX", "MIA", "IAH", "BOS", "EWR"]) + \
    b"</Ground_Stop_List></Delay_type></AIRPORT_STATUS_INFORMATION>"

FAA_WEATHER = b"""<AIRPORT_STATUS_INFORMATION><Update_Time>x</Update_Time><Delay_type><Name>Ground Stop Programs</Name>
<Ground_Stop_List><Program><ARPT>EWR</ARPT><Reason>thunderstorms</Reason><End_Time>5:30 pm EST</End_Time></Program>
<Program><ARPT>LGA</ARPT><Reason>low ceilings</Reason><End_Time>6:00 pm EST</End_Time></Program></Ground_Stop_List>
</Delay_type></AIRPORT_STATUS_INFORMATION>"""

TODAY = ("FAA grounds all flights across U.S. due to communications problems - The Washington Times",
         "Flights are halted at airports across the United States due to unspecified 'extraordinary circumstances,' FAA says - KSAT",
         "FAA issues nationwide ground stop citing communication issues affecting 'multiple' air traffic control facilities - CNBC",
         "FAA nationwide ground stop issued due to communication problem - KCRA")


def run(results, state=None, series=None, first_run=False, when=NOW):
    state = {} if state is None else state
    return S.evaluate(results, series or [], when, state, first_run, baseline) + (state,)


def test_faa_parser():
    f = S.parse_faa(FAA_NATIONAL)
    assert f["ok"] and f["airports"] == 16 and S.official_signal(f)[0]
    w = S.parse_faa(FAA_WEATHER)
    assert w["ok"] and w["airports"] == 2 and not S.official_signal(w)[0]
    assert S.parse_faa(b'{"error": 1}')["ok"] is False
    assert S.parse_faa(b"<AIRPORT_STATUS_INFORMATION>NAS-wide ground stop</AIRPORT_STATUS_INFORMATION>")["national"]


def test_replay_13_nov_us_ground_stop_news_and_official_confirm():
    out, alerts, _ = run({"faa": S.parse_faa(FAA_NATIONAL), "news_watch": {"watch": news(*TODAY)}})
    us = [s for s in out["shocks"] if s["code"] == "US" and s["sector"] == "aviation"]
    assert us and us[0]["level"] == "Confirmed" and set(us[0]["signals"]) == {"official", "news"}
    a = [a for a in alerts if a["key"] == "shock-US-aviation"]
    assert a and a[0]["channels"] == ["proactive"] and a[0]["priority"] == 4
    assert "United States" in a[0]["title"] and "flights" in a[0]["title"]


def test_news_alone_confirms_nothing_until_it_persists():
    state = {}
    _, alerts, state = run({"news_watch": {"watch": news(*TODAY)}}, state)
    assert not alerts  # one signal: wait
    _, alerts, state = run({"news_watch": {"watch": news(*TODAY)}}, state, when=NOW + timedelta(minutes=30))
    assert [a["title"] for a in alerts] == ["UNCONFIRMED: possible flights grounded nationwide (United States)"]


def test_traffic_and_news_confirm_and_then_all_clear():
    series, state = history(), {}
    res = {"aircraft": {"traffic_by_hub": traffic(0.35, US_HUBS)}, "news_watch": {"watch": news(*TODAY)}}
    _, alerts, state = run(res, state, series)  # traffic needs a second run in a row
    _, alerts, state = run(res, state, series, when=NOW + timedelta(minutes=15))
    hit = [a for a in alerts if a["key"] == "shock-US-aviation"]
    assert hit and not hit[0]["title"].startswith("UNCONFIRMED")
    calm = {"aircraft": {"traffic_by_hub": traffic(1.0)}, "news_watch": {"watch": {}}}
    _, alerts, state = run(calm, state, series, when=NOW + timedelta(minutes=30))
    assert not alerts  # wait a little before calling it over
    _, alerts, state = run(calm, state, series, when=NOW + timedelta(minutes=65))
    assert [a["title"] for a in alerts] == ["Update: United States flights - appears to be over"]


def test_australian_halt_goes_to_both_channels():
    au = ("Airservices Australia outage grounds all flights nationwide - ABC News",
          "All flights grounded across Australia after air traffic control failure - SMH",
          "Air traffic control outage halts flights nationwide in Australia - 9News")
    series = history()
    res = {"aircraft": {"traffic_by_hub": traffic(0.3, AU_HUBS)}, "news_watch": {"watch": news(*au)}}
    state = {}
    run(res, state, series)
    _, alerts, _ = run(res, state, series, when=NOW + timedelta(minutes=15))
    a = [a for a in alerts if a["key"] == "shock-AU-aviation"]
    assert a and a[0]["channels"] == ["proactive", "reactive"] and a[0]["priority"] == 5


# ---------- false alarms that must stay quiet ----------
def test_weather_ground_stops_and_single_airline_are_ignored():
    noise = ("FAA issues ground stop at Newark due to thunderstorms - NBC New York",
             "US FAA issues ground stop for all JetBlue planes - Reuters",
             "Delta flights grounded after IT glitch - CNN",
             "Ground stop at LaGuardia as low clouds roll in - ABC7")
    out, alerts, _ = run({"faa": S.parse_faa(FAA_WEATHER), "news_watch": {"watch": news(*noise)}})
    assert not out["shocks"] and not alerts


def test_two_outlets_or_old_stories_do_not_count():
    two = news(*TODAY[:2])
    old = {"aviation_halt": {"items": [item(t, 60 * 6) for t in TODAY]}}
    utah = news("When Israel's airspace closed, this Utah tour company got its travelers home - KSL",
                "Israel airspace closed lessons for travellers - Travel Weekly", "Israel closes airspace anniversary - Times")
    for w in (two, old, utah):
        out, alerts, _ = run({"news_watch": {"watch": w}})
        assert not out["shocks"] and not alerts


def test_lifted_reports_switch_news_off():
    lifted = news("FAA lifts nationwide ground stop, flights resume - CNN", "US flights resume as FAA lifts ground stop - AP",
                  "FAA ends nationwide ground stop - Reuters", start=1)
    w = {"aviation_halt": {"items": lifted["aviation_halt"]["items"] + news(*TODAY, start=40)["aviation_halt"]["items"]}}
    out, _, _ = run({"news_watch": {"watch": w}})
    assert not [s for s in out["shocks"] if s["code"] == "US"]


def test_tracking_glitch_across_many_countries_is_ignored():
    series = history()
    res = {"aircraft": {"traffic_by_hub": traffic(0.2)}}  # every hub at once: the tracker, not the world
    state = {}
    run(res, state, series)
    out, alerts, _ = run(res, state, series, when=NOW + timedelta(minutes=15))
    assert out["tracking_glitch"] and not out["shocks"] and not alerts


def test_one_quiet_airport_area_is_board_only():
    series = history()
    res = {"aircraft": {"traffic_by_hub": traffic(0.2, ["Moscow"])}}  # drone closures around Moscow
    state = {}
    for m in (0, 15, 45, 90):
        out, alerts, state = run(res, state, series, when=NOW + timedelta(minutes=m))
        assert not alerts
    assert [s["code"] for s in out["shocks"]] == ["RU"]


def test_one_us_hub_down_is_not_national():
    series = history()
    res = {"aircraft": {"traffic_by_hub": traffic(0.3, ["Palm Beach / Miami"])}}  # a hurricane in Florida
    state = {}
    for m in (0, 15, 45):
        out, alerts, state = run(res, state, series, when=NOW + timedelta(minutes=m))
    assert not out["shocks"] and not alerts


def test_learning_baseline_is_silent():
    out, alerts, _ = run({"aircraft": {"traffic_by_hub": traffic(0.1)}})
    assert out["traffic"]["US"]["status"] == "learning baseline" and not alerts


def test_bootstrap_run_never_pushes():
    _, alerts, _ = run({"faa": S.parse_faa(FAA_NATIONAL), "news_watch": {"watch": news(*TODAY)}}, first_run=True)
    assert not alerts


def test_no_repeat_within_cooldown():
    state = {}
    res = {"faa": S.parse_faa(FAA_NATIONAL), "news_watch": {"watch": news(*TODAY)}}
    _, a1, state = run(res, state)
    _, a2, state = run(res, state, when=NOW + timedelta(minutes=15))
    assert len(a1) == 1 and not a2
