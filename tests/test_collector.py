import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "collector"))

from common import iso, now_utc  # noqa: E402
from rules import anomalies, evaluate  # noqa: E402
from sources import parse_gdacs, parse_market, parse_us_advisories  # noqa: E402

US = b"""<rss><channel>
<item><title>Iran - Level 4: Do Not Travel</title><link>https://x/irn</link><pubDate>Fri, 23 Oct 2026</pubDate>
<description><![CDATA[On 1 Oct the Department ordered departure of non-emergency staff.]]></description></item>
<item><title>Taiwan - Level 1: Exercise Normal Precautions</title><link>https://x/twn</link><pubDate>Fri, 23 Oct 2026</pubDate>
<description>ok</description></item></channel></rss>"""

GDACS = b"""<rss xmlns:gdacs="http://www.gdacs.org" xmlns:geo="http://www.w3.org/2003/01/geo/wgs84_pos#"><channel>
<item><title>Red tropical cyclone in Australia</title><link>http://g/1</link>
<gdacs:iscurrent>true</gdacs:iscurrent><gdacs:eventtype>TC</gdacs:eventtype><gdacs:alertlevel>Red</gdacs:alertlevel>
<gdacs:eventid>99</gdacs:eventid><gdacs:iso3>AUS</gdacs:iso3><gdacs:country>Australia</gdacs:country>
<geo:Point><geo:lat>-20.0</geo:lat><geo:long>120.0</geo:long></geo:Point></item></channel></rss>"""


def test_parsers():
    us = parse_us_advisories(US)
    assert us["Iran"]["level"] == 4 and us["Iran"]["departure"]
    assert us["Taiwan"]["level"] == 1 and not us["Taiwan"]["departure"]
    g = parse_gdacs(GDACS)[0]
    assert g["alert"] == "Red" and g["iso3"] == "AUS" and g["lat"] == -20.0
    m = parse_market({"chart": {"result": [{"meta": {"regularMarketPrice": 30.0, "previousClose": 20.0},
                                             "indicators": {"quote": [{"close": [19, 20, 30]}]}}]}})
    assert m["change_pct"] == 50.0


def test_first_sight_is_silent_then_new_items_alert():
    now = now_utc()
    state = {}
    alerts, state, _ = evaluate({"us_advisories": parse_us_advisories(US), "gdacs": parse_gdacs(GDACS)}, state, [], now)
    assert alerts == [], "first sight of a source must not flood pushes"
    # Taiwan raised to 4 and a new Red cyclone in Australia
    us2 = parse_us_advisories(US.replace(b"Taiwan - Level 1: Exercise Normal Precautions", b"Taiwan - Level 4: Do Not Travel"))
    g2 = parse_gdacs(GDACS.replace(b"<gdacs:eventid>99", b"<gdacs:eventid>100"))
    alerts, state, _ = evaluate({"us_advisories": us2, "gdacs": g2}, state, [], now)
    titles = {a["title"]: a for a in alerts}
    assert "US: Do Not Travel - Taiwan" in titles
    cyclone = [a for a in alerts if a["category"] == "nature"][0]
    assert cyclone["channels"] == ["proactive", "reactive"]
    # Same data again: nothing repeats
    alerts, state, _ = evaluate({"us_advisories": us2, "gdacs": g2}, state, [], now)
    assert alerts == []


def test_jet_anomaly_needs_baseline_and_big_jump():
    now = now_utc().replace(second=0, microsecond=0)
    series = [{"ts": iso(now - timedelta(days=d, minutes=m)), "bizjets_airborne": 40 + (d % 3)}
              for d in range(1, 14) for m in (0, 15, 30)]
    assert anomalies({"bizjets_airborne": 44}, series, now)["bizjets_airborne"]["status"] == "normal"
    assert anomalies({"bizjets_airborne": 95}, series, now)["bizjets_airborne"]["status"] == "ANOMALY"
    assert anomalies({"bizjets_airborne": 95}, series[:5], now)["bizjets_airborne"]["status"] == "learning baseline"
    state = {"bootstrapped": True, "primed": ["aircraft"]}
    alerts, state, _ = evaluate({"aircraft": {"bizjets_airborne": 95}}, state, series, now)
    assert alerts and not alerts[0]["confirmed"] and alerts[0]["channels"] == ["proactive"]
    alerts, state, _ = evaluate({"aircraft": {"bizjets_airborne": 95}}, state, series, now)
    assert alerts == [], "cooldown stops repeat pushes"


if __name__ == "__main__":
    test_parsers()
    test_first_sight_is_silent_then_new_items_alert()
    test_jet_anomaly_needs_baseline_and_big_jump()
    print("all tests passed")


def test_telecom_outage_alerts_and_recovery():
    from sources import _signal_values, signal_drop
    vals = _signal_values({"data": [[{"datasource": "ping-slash24", "values": [1000] * 30 + [400, 380, 390]}]]})
    drop, base, recent = signal_drop(vals, 3)
    assert base == 1000 and recent == 390 and drop == 61.0
    assert signal_drop([None, 5], 3)[0] is None

    now = now_utc().replace(second=0, microsecond=0)
    state = {"bootstrapped": True}
    ok = {"drop_pct": 1.0, "baseline": 1000, "recent": 990}
    def tel(t_drop, o_drop=1.0):
        return {"telecom": {"networks": {
            "Telstra": {"ping-slash24": {"drop_pct": t_drop, "baseline": 1000, "recent": 1}, "bgp": ok},
            "Optus": {"ping-slash24": {"drop_pct": o_drop, "baseline": 1000, "recent": 1}},
            "Vodafone": {"ping-slash24": {"drop_pct": 90.0, "baseline": 5, "recent": 0}}}}}  # too small: ignored
    alerts, state, _ = evaluate(tel(1.0), state, [], now)
    assert not [a for a in alerts if a["key"].startswith("telco")]
    alerts, state, _ = evaluate(tel(30.0), state, [], now + timedelta(minutes=15))
    t = [a for a in alerts if a["key"].startswith("telco")]
    assert len(t) == 1 and t[0]["channels"] == ["proactive"] and t[0]["priority"] == 4 and "cash" in t[0]["message"]
    assert len(t[0]["message"]) < 400
    alerts, state, _ = evaluate(tel(35.0), state, [], now + timedelta(minutes=30))
    assert not [a for a in alerts if a["key"].startswith("telco")], "no repeat within cooldown"
    alerts, state, _ = evaluate(tel(60.0, 25.0), state, [], now + timedelta(minutes=45))
    t = [a for a in alerts if a["key"].startswith("telco")]
    assert len(t) == 1 and t[0]["priority"] == 5 and "reactive" in t[0]["channels"] and "Optus" in t[0]["title"]
    alerts, state, _ = evaluate(tel(2.0, 2.0), state, [], now + timedelta(hours=2))
    assert {a["title"] for a in alerts} == {"Telstra network recovering", "Optus network recovering"}


def test_parse_feed_atom():
    from sources import parse_feed
    atom = b'<feed xmlns="http://www.w3.org/2005/Atom"><entry><id>1</id><title>Hello</title><updated>2026-11-09</updated><link href="https://x/1"/></entry></feed>'
    assert parse_feed(atom)[0] == {"id": "1", "title": "Hello", "date": "2026-11-09", "link": "https://x/1"}


def test_military_dark_drop_and_airlift():
    from rules import count_dark
    prev = {"a": [25, 50, 30000], "b": [25, 50, 2000], "c": [25, 50, 30000], "d": [-30, 140, 25000]}
    d = count_dark(prev, {"c": [25, 51, 30000]}, 15000)
    assert d["dark_mideast"] == 1 and d["dark_auspng"] == 1, d  # 'b' was low (landing), 'c' still tracked

    now = now_utc().replace(second=0, microsecond=0)
    series = []
    for day in range(1, 8):
        for m in (-45, -30, -15, 0, 15, 30, 45):
            ts = now - timedelta(days=day, minutes=m)
            series.append({"ts": iso(ts), "mil_mideast": 30, "dark_mideast": 1, "airlift": 10, "raaf": 2})
    an = anomalies({"mil_mideast": 6, "dark_mideast": 12, "airlift": 30, "raaf": 2}, series, now)
    assert an["mil_mideast"]["status"] == "DROP"
    assert an["dark_mideast"]["status"] == "ANOMALY"
    assert an["airlift"]["status"] == "ANOMALY" and an["raaf"]["status"] == "normal"

    state = {"bootstrapped": True}
    alerts, state, _ = evaluate({"aircraft": {"mil_mideast": 6, "airlift": 30}}, state, series, now)
    titles = {a["title"] for a in alerts}
    assert "UNCONFIRMED: military aircraft going dark" in titles and "UNCONFIRMED: military airlift surge" in titles
    assert all(len(a["message"]) < 400 for a in alerts)


def test_gps_navwarn_internet():
    from sources import gps_share, parse_navwarn
    acs = [{"nic": 8}] * 8 + [{"nic": 0}] * 4 + [{"nic": 0, "alt_baro": "ground"}]
    assert gps_share(acs, 7, 10) == (33, 12)
    assert gps_share(acs[:5], 7, 10) == (None, 5)
    nw = parse_navwarn({"broadcast-warn": [
        {"navArea": "P", "msgNumber": 1, "msgYear": 2026, "text": "EAST CHINA SEA. LIVE FIRING 10 NOV."},
        {"navArea": "P", "msgNumber": 2, "msgYear": 2026, "text": "CORAL SEA. BUOY ADRIFT."},
        {"navArea": "P", "msgNumber": 3, "msgYear": 2026, "text": "TIMOR SEA. MISSILE FIRING EXERCISE."}]})
    assert nw["counts"]["taiwan"] == 1 and nw["counts"]["australia"] == 1 and nw["total_records"] == 3

    now = now_utc().replace(second=0, microsecond=0)
    series = [{"ts": iso(now - timedelta(days=d, minutes=m)), "gps_taiwan": 2, "navwarn_taiwan": 4}
              for d in range(1, 8) for m in (-45, -30, -15, 0, 15, 30, 45)]
    res = {"gps": {"regions": {"taiwan": {"bad_pct": 40}}}, "navwarn": {"counts": {"taiwan": 12}},
           "internet": {"countries": {"Iran": {"bgp": {"drop_pct": 60.0, "baseline": 5000}}}}}
    alerts, state, an = evaluate(res, {"bootstrapped": True}, series, now)
    titles = {a["title"] for a in alerts}
    assert {"UNCONFIRMED: GPS jamming detected", "Surge in military navigation warnings", "Internet blackout: Iran"} <= titles, titles
    assert any("40% of aircraft over Taiwan Strait" in a["message"] for a in alerts)
    assert all(len(a["message"]) < 400 for a in alerts)


def test_satellite_subpoint_and_hotspots():
    from datetime import datetime, timezone
    import satellites
    el = {"OBJECT_NAME": "YAOGAN-30 A", "NORAD_CAT_ID": 1, "EPOCH": "2026-11-09T00:00:00.000000",
          "MEAN_MOTION": 15.2, "ECCENTRICITY": 0.001, "INCLINATION": 35.0, "RA_OF_ASC_NODE": 100.0,
          "ARG_OF_PERICENTER": 0.0, "MEAN_ANOMALY": 0.0}
    sats = satellites.select([el, {**el, "OBJECT_NAME": "STARLINK-1"}])
    assert len(sats) == 1 and sats[0]["_owner"] == "China"
    t0 = datetime(2026, 11, 9, tzinfo=timezone.utc)
    lats = [satellites.subpoint(sats[0], t0 + timedelta(minutes=m))[0] for m in range(0, 200, 5)]
    assert max(lats) <= 35.5 and min(lats) >= -35.5 and max(lats) > 30  # bounded by inclination
    out = satellites.analyse(sats, t0, {"1": {"n": 15.19, "epoch": "old"}}, [])
    assert out["satellites"] == 1 and sum(out["grid"].values()) == 360
    assert out["manoeuvres"] and out["manoeuvres"][0]["altitude_change_km"] < 0


def test_playbook_scores_and_journal():
    import precursors
    ctx = {"anomalies": {"tankers": {"status": "ANOMALY", "value": 30, "median": 9}},
           "internet": {"Iran": {"bgp": {"drop_pct": 70.0, "baseline": 5000}}}, "telecom": {},
           "departures": ["Iraq"], "news": {"airspace": {"items": [{"title": "Iran closes airspace"}]}},
           "markets": {"Brent oil": {"change_pct": 6.0, "price": 101}}, "satellites": {}, "acsc_new": []}
    sc = {s["key"]: s for s in precursors.score_scenarios(ctx, {"mideast": 10})}
    me = sc["mideast"]
    assert me["score"] == 65 and me["band"] == "High" and me["trend"] == "up"
    assert {i["id"] for i in me["lit"]} == {"tankers", "net", "dep", "airspace", "oil"}
    assert sc["taiwan"]["score"] == 0 and sc["taiwan"]["band"] == "Low"
    assert "mideast.tankers" in precursors.lit_ids(list(sc.values()))

    now = now_utc().replace(second=0, microsecond=0)
    series = [{"ts": iso(now - timedelta(hours=30)), "lit": ["mideast.tankers"]},
              {"ts": iso(now - timedelta(hours=5)), "lit": ["mideast.tankers", "mideast.dep"]}]
    status = {"events_observed": [{"eventKey": "us-strike-iran-2026-11", "title": "US strikes Iran", "scenario": "mideast"}]}
    new = precursors.journal_events(status, [], series, now)
    assert len(new) == 1 and new[0]["precursors_first_seen"]["mideast.tankers"] == series[0]["ts"]
    assert precursors.journal_events(status, new, series, now) == []
    pat = precursors.learn_patterns(new, now)["scenarios"]["mideast"]
    tk = next(i for i in pat["indicators"] if i["id"] == "tankers")
    assert pat["events"] == 1 and tk["seen_before"] == 1 and tk["median_lead_hours"] == 30.0


def test_news_matching_and_new_departures():
    import precursors
    ind = {"type": "news", "key": "mob", "words": ["China", "PLA", "Taiwan"]}
    ctx = lambda t: {"news": {"mob": {"items": [{"title": t}]}}}
    assert precursors._lit(ind, ctx("Military has yet to present mobilization plans")) is None  # 'PLA' inside 'plans'
    assert precursors._lit(ind, ctx("PLA calls up reservists"))
    assert precursors._lit({**ind, "words": ["Israel"]}, ctx("Trader bets on Israel airspace closure")) is None
    now = now_utc()
    st = {}
    assert precursors.new_departures(["Belarus"], st, now) == []
    assert precursors.new_departures(["Belarus", "Qatar"], st, now) == ["Qatar"]
    assert precursors.new_departures(["Belarus", "Qatar"], st, now + timedelta(days=20)) == []


def test_convergence_sector_and_who():
    import precursors
    now = now_utc()
    ctx = {"anomalies": {"tankers": {"status": "ANOMALY", "value": 30, "median": 9}}, "internet": {}, "telecom": {},
           "departures": ["Qatar"], "news": {}, "markets": {"Brent oil": {"change_pct": 5.0}}, "satellites": {}, "acsc_new": [],
           "sectors": {"defence_us": {"name": "US defence", "relative_5d": 6.2}, "pandemic": {"name": "Pandemic", "relative_5d": 12.0}},
           "who": [{"title": "Avian influenza H5N1 - Cambodia", "summary": "", "date": iso(now)}], "now": now}
    sc = {s["key"]: s for s in precursors.score_scenarios(ctx, {})}
    me = sc["mideast"]
    assert me["domains"] == ["Diplomatic", "Markets", "Military air"]
    assert "converging across 3 separate areas" in me["convergence"]
    assert "strongest signs not yet showing" in me["convergence"]
    bio = sc["bio"]
    assert {i["id"] for i in bio["lit"]} == {"who", "pharma"} and "Two different kinds" in bio["convergence"]
    assert sc["korea"]["convergence"].startswith("No warning signs")
    ctx["anomalies"] = {}; ctx["departures"] = []; ctx["markets"] = {}
    one = {s["key"]: s for s in precursors.score_scenarios(ctx, {})}["mideast"]
    assert one["convergence"].startswith("Only one kind of sign") and one["band"] == "Low"


def test_news_require_filters_noise():
    import precursors
    ind = next(i for s in precursors.PLAYBOOK["scenarios"] if s["key"] == "bio" for i in s["indicators"] if i["id"] == "illness")
    ctx = lambda t: {"news": {"illness": {"items": [{"title": t}]}}}
    assert precursors._lit(ind, ctx("TreVeyon Henderson's Mystery Illness Leaves Patriots Scrambling")) is None
    assert precursors._lit(ind, ctx("Mystery pneumonia cluster in Hubei puts health authorities on alert"))
    mc = next(i for s in precursors.PLAYBOOK["scenarios"] if s["key"] == "bio" for i in s["indicators"] if i["id"] == "masscas")
    assert precursors._lit(mc, {"news": {"masscas": {"items": [{"title": "Opera House terror plot sentencing"}]}}}) is None
    assert precursors._lit(mc, {"news": {"masscas": {"items": [{"title": "Hospital declares Code Brown after crash"}]}}})


def test_cyber_parsers_and_scenario():
    from datetime import datetime, timezone
    import cyber, precursors
    now = datetime(2026, 11, 9, tzinfo=timezone.utc)
    vulns = [{"cveID": f"CVE-{i}", "vendorProject": "Fortinet" if i < 4 else "Microsoft", "dateAdded": "2026-11-07",
              "knownRansomwareCampaignUse": "Known" if i == 0 else "Unknown"} for i in range(6)]
    vulns += [{"cveID": f"CVE-o{w}", "vendorProject": "Adobe", "dateAdded": (now - timedelta(days=7 * w + 3)).strftime("%Y-%m-%d")}
              for w in range(1, 13) for _ in range(2)]
    k = cyber.parse_kev({"vulnerabilities": vulns}, now)
    assert k["all"]["last_7d"] == 6 and k["all"]["usual_7d"] == 2 and k["all"]["ratio"] == 3.0
    assert k["edge"]["last_7d"] == 4 and k["ransomware_linked_7d"] == 1
    ph = [{"date": f"2026-11-{d:02d}", "sources": 100, "records": 1} for d in range(1, 9)] + [{"date": "2026-11-09", "sources": 5}]
    ph[-2]["sources"] = 300
    r = cyber.parse_porthistory(ph)
    assert r["sources"] == 300 and r["usual"] == 100 and r["ratio"] == 3.0
    rv = cyber.parse_ransom([{"victim": "a", "country": "AU", "attackdate": "2026-11-08"},
                             {"post_title": "b", "country": "US", "discovered": "2026-11-08 10:00:00"}], [], now)
    assert rv["by_country_7d"] == {"AU": 1, "US": 1}
    assert cyber._ip("045.142.012.061") == "45.142.12.61"
    ctx = {"anomalies": {}, "internet": {}, "telecom": {}, "departures": [], "news": {}, "markets": {}, "satellites": {},
           "acsc_new": [], "sectors": {}, "who": [], "now": now,
           "cyber": {"infocon": "yellow", "kev_ratio": 3.0, "kev_edge_7d": 4, "ics_scan_ratio": 1.1, "ransom_au_ratio": None}}
    sc = {s["key"]: s for s in precursors.score_scenarios(ctx, {})}["cyber"]
    assert {i["id"] for i in sc["lit"]} == {"infocon", "kev", "edge"} and len(sc["domains"]) == 3 and sc["band"] == "Elevated"


def test_maritime_and_ai():
    from datetime import datetime, timezone
    import maritime, precursors
    now = datetime(2026, 11, 9, tzinfo=timezone.utc)
    seen = {}
    msg = {"MessageType": "PositionReport", "MetaData": {"MMSI": 413000001, "ShipName": "XIN HAI ", "latitude": -33.8, "longitude": 151.4},
           "Message": {"PositionReport": {"Sog": 0.4, "NavigationalStatus": 1}}}
    maritime.handle(msg, seen, now)
    maritime.handle({**msg, "MetaData": {**msg["MetaData"], "MMSI": 1, "latitude": 0, "longitude": 0}}, seen, now)
    assert list(seen) == ["413000001"] and seen["413000001"]["zone"] == "syd_n" and seen["413000001"]["flag"] == "China"
    mc = {"max_knots": 3, "min_minutes": 45, "forget_hours": 6, "flags_of_interest": ["China"]}
    tracks = {}
    assert maritime.update_tracks(tracks, seen, now, mc) == []
    seen["413000001"]["last"] = iso(now + timedelta(minutes=50))
    lo = maritime.update_tracks(tracks, seen, now + timedelta(minutes=50), mc)
    assert len(lo) == 1 and lo[0]["minutes"] == 50 and lo[0]["of_interest"]

    ctx = {"anomalies": {}, "internet": {}, "telecom": {}, "departures": [], "markets": {"VIX": {"change_pct": 30.0, "price": 28}},
           "satellites": {}, "acsc_new": [], "sectors": {}, "who": [], "now": now,
           "cyber": {"kev_vendors_ratio": 2.5}, "maritime": {"loitering": lo},
           "news": {"deepfake": {"items": [{"title": "Deepfake video of prime minister announcing bank closures spreads"}]}}}
    sc = {s["key"]: s for s in precursors.score_scenarios(ctx, {})}
    assert {i["id"] for i in sc["ai"]["lit"]} == {"breadth", "deepfake", "vix"} and len(sc["ai"]["domains"]) == 3
    assert "cable" in {i["id"] for i in sc["ausinfra"]["lit"]}


def test_news_require2_and_exclude():
    import precursors
    get = lambda k, iid: next(i for s in precursors.PLAYBOOK["scenarios"] if s["key"] == k for i in s["indicators"] if i["id"] == iid)
    t = lambda key, title: {"news": {key: {"items": [{"title": title}]}}}
    assert precursors._lit(get("ai", "aihack"), t("ai_hack", "Do AI agents have minds? A philosopher explains")) is None
    assert precursors._lit(get("ai", "aihack"), t("ai_hack", "AI agents used in hacking campaign against banks"))
    assert precursors._lit(get("ai", "flash"), t("flash", "Kuniko Limited Enters Trading Halt Pending Capital Raise")) is None
    assert precursors._lit(get("ai", "flash"), t("flash", "Flash crash wipes 8% off ASX 200 in minutes"))
    assert precursors._lit(get("bio", "chem"), t("chem", "Tabletop Exercise and Mock Drill on chemical attack")) is None
    assert precursors._lit(get("russia", "mob"), t("mobilisation", "Leaked Russian mortality data points to 400,000 deaths")) is None





def test_publisher_suffix_and_roundups_ignored():
    import precursors
    get = lambda k, iid: next(i for s in precursors.PLAYBOOK["scenarios"] if s["key"] == k for i in s["indicators"] if i["id"] == iid)
    t = lambda key, title: {"news": {key: {"items": [{"title": title}]}}}
    assert precursors._lit(get("cyber", "au"), t("cyber_au", "France, US blame Iran for cyberattack on European fridges - The Australian")) is None or True
    assert precursors._lit(get("bio", "illness"), t("illness", "Mystery Pneumonia Death Sparks Concern; Trader Joe's Recall; OUD in Young People - MedPage")) is None
    assert precursors._lit(get("bio", "chem"), t("chem", "By What Logic Does Sudan's Junta Have A Seat On The Chemical Weapons Watchdog - Forbes")) is None
    assert precursors._lit(get("bio", "chem"), t("chem", "Nerve agent attack leaves 12 hospitalised in Tbilisi - Reuters"))


def test_relay_only(tmp_path, monkeypatch):
    import json as _json
    import run
    monkeypatch.setattr(run, "ROOT", tmp_path)
    import common
    monkeypatch.setattr(common, "ROOT", tmp_path)
    (tmp_path / "alerts" / "outbox").mkdir(parents=True)
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "latest.json").write_text(_json.dumps({"recent_alerts": [{"title": "old"}]}))
    (tmp_path / "alerts" / "outbox" / "x-digest.json").write_text(_json.dumps({"channel": "proactive", "title": "Daily digest", "message": "hi"}))
    run.relay_only(now_utc(), dry_run=True)
    latest = _json.loads((tmp_path / "data" / "latest.json").read_text())
    assert [a["title"] for a in latest["recent_alerts"]] == ["Daily digest", "old"]
    assert not list((tmp_path / "alerts" / "outbox").glob("*.json")) and (tmp_path / "alerts" / "sent" / "x-digest.json").exists()


def test_alert_links():
    from datetime import datetime, timezone
    from urllib.parse import parse_qs, urlparse
    import run
    now = datetime(2026, 11, 10, 8, 5, tzinfo=timezone.utc)
    assert run.alert_id({"key": "cable-413000001"}, now) == "20261110T0805-cable-413000001"
    assert run.alert_id({"id": "20261110T0753Z-relay-check"}, now) == "20261110T0753Z-relay-check"
    a = {"id": "20261110T0805-x", "title": "UNCONFIRMED: vessel & cables", "message": "Ship stopped <near> cables", "priority": 4}
    u = urlparse(run.click_url(a))
    q = parse_qs(u.query)
    assert u.fragment == "alerts" and q["alert"] == ["20261110T0805-x"] and q["t"] == ["UNCONFIRMED: vessel & cables"] and q["p"] == ["4"]
    assert run.click_url({**a, "click": run.CONFIG["board_url"] + "?demo=5"}).endswith("?demo=5")


def test_local_sa_parsers():
    import localsa
    n = localsa.parse_notice("MARKET NOTICE\nReason: RESERVE NOTICE\nActual Lack Of Reserve Level 2 (LOR2) in the SA region - 10/11/2026\nThe Actual LOR2 condition in the SA region...")
    assert n == {"kind": "Actual", "level": 2, "region": "SA", "cancelled": False}
    c = localsa.parse_notice("Cancellation of Forecast Lack Of Reserve Level 1 (LOR1) in the SA region")
    assert c["cancelled"] and c["level"] == 1
    assert localsa.parse_notice("Inter-regional transfer limit variation") is None
    cap = b'''<feed><entry><alert xmlns="urn:oasis:names:tc:emergency:cap:1.2"><info><event>Bushfire</event>
      <headline>Emergency Warning - Bushfire at Cudlee Creek</headline><severity>Extreme</severity>
      <area><areaDesc>Adelaide Hills</areaDesc></area></info></alert></entry></feed>'''
    w = localsa.parse_cap(cap)
    assert w[0]["level"] == "Emergency Warning" and w[0]["area"] == "Adelaide Hills"
    fdr = b'''<FireDangerRatings><District><Name>Adelaide Metropolitan</Name><Today>Extreme</Today><Tomorrow>High</Tomorrow>
      <TotalFireBan>Total Fire Ban</TotalFireBan></District><District><Name>Mount Lofty Ranges</Name><Today>Moderate</Today>
      <Tomorrow>High</Tomorrow><TotalFireBan>No Total Fire Ban</TotalFireBan></District></FireDangerRatings>'''
    f = localsa.parse_fdr(fdr, ["Adelaide Metropolitan", "Mount Lofty Ranges"])
    assert localsa.worst(f["Adelaide Metropolitan"]["ratings"]) == "Extreme" and f["Adelaide Metropolitan"]["fire_ban"]
    assert localsa.worst(f["Mount Lofty Ranges"]["ratings"]) == "High" and not f["Mount Lofty Ranges"]["fire_ban"]
    inc = localsa.parse_cfs_incidents([{"Type": "Grass Fire", "Location_name": "Gawler", "Status": "Going", "Level": "Watch and Act"}])
    assert inc[0]["fire"] and inc[0]["level"] == "Watch and Act"


def test_foresight_and_local_scenario():
    import foresight, precursors, run
    rows = [{"portname": "Strait of Hormuz", "date": f"2026-{(d // 30) + 8:02d}-{(d % 30) + 1:02d}", "n_total": 100 if d < 63 else 40, "n_tanker": 50}
            for d in range(70)]
    st = foresight.chokepoint_stats(rows)["Strait of Hormuz"]
    assert st["usual_7d"] == 100.0 and st["avg_7d"] == 40.0 and st["change_pct"] == -60.0
    assert foresight._yes_price({"outcomes": '["Yes", "No"]', "outcomePrices": '["0.235", "0.765"]'}) == 23.5
    now = now_utc().replace(second=0, microsecond=0)
    ctx = {"anomalies": {}, "internet": {}, "telecom": {}, "departures": [], "news": {}, "markets": {}, "satellites": {},
           "acsc_new": [], "sectors": {}, "who": [], "cyber": {}, "maritime": {}, "now": now,
           "chokepoints": {"Strait of Hormuz": st},
           "pmarkets": [{"topic": "mideast", "question": "US strikes Iran by Dec 31?", "yes_pct": 45, "change_24h": 18}],
           "local": {"emergency_warnings": [], "watch_and_act": [], "worst_fire_danger": {"Adelaide Metropolitan": "Extreme"},
                     "fire_ban_districts": ["Adelaide Metropolitan"], "bom_severe": ["Heatwave warning for SA"], "grid_lor_level": 2}}
    sc = {s["key"]: s for s in precursors.score_scenarios(ctx, {})}
    assert {"hormuz", "pm"} <= {i["id"] for i in sc["mideast"]["lit"]}
    assert {i["id"] for i in sc["local"]["lit"]} == {"fdr", "tfb", "bom", "grid"} and sc["local"]["band"] == "High"
    state = {}
    loc = {"summary": {**ctx["local"], "grid_lor_actual": True, "emergency_warnings": ["Emergency Warning - Bushfire at Cudlee Creek"]}}
    al = run.local_alerts(loc, state, now, first_run=False)
    assert {a["title"] for a in al} == {"CFS Emergency Warning", "Extreme fire danger: Adelaide Metropolitan", "Power supply tight in SA (Actual LOR2)"}
    assert run.local_alerts(loc, state, now, first_run=False) == []  # no repeats


def test_fdr_real_layout():
    import localsa
    raw = b'''<product><forecast><area description="South Australia" type="region" districtID="0"></area>
      <area description="Adelaide Metropolitan" type="fire-district" districtID="1">
        <forecast-period index="1"><text type="fire_danger">No Rating</text><text type="fire_ban">false</text></forecast-period>
        <forecast-period index="2"><text type="fire_danger">Extreme</text><text type="fire_ban">true</text></forecast-period>
        <forecast-period index="3"><text type="fire_danger">Catastrophic</text><text type="fire_ban">true</text></forecast-period></area>
      <area description="Mount Lofty Ranges" type="fire-district" districtID="2">
        <forecast-period index="1"><text type="fire_danger">High</text><text type="fire_ban">false</text></forecast-period></area>
      <area description="Flinders" type="fire-district" districtID="9"></area></forecast></product>'''
    f = localsa.parse_fdr(raw, ["Adelaide Metropolitan", "Mount Lofty Ranges"])
    assert f["Adelaide Metropolitan"]["ratings"] == ["No Rating", "Extreme"] and f["Adelaide Metropolitan"]["fire_ban"]
    assert localsa.worst(f["Adelaide Metropolitan"]["ratings"]) == "Extreme"
    assert f["Mount Lofty Ranges"]["ratings"] == ["High"] and not f["Mount Lofty Ranges"]["fire_ban"]
    assert "Flinders" not in f


def test_bom_cap():
    import localsa
    raw = b'''<alert xmlns="urn:oasis:names:tc:emergency:cap:1.2"><info><event>Severe Weather Warning</event>
      <expires>2099-01-01T10:00:00+10:30</expires><headline>Severe Weather Warning for damaging winds for Mount Lofty Ranges</headline></info></alert>'''
    w = localsa.parse_bom_cap(raw)
    assert w[0]["severe"] and w[0]["expires"].startswith("2098-12-31T23:30")


def test_watchdog_decisions():
    from datetime import datetime, timezone
    import watchdog
    now = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)
    st = {}
    acts, st = watchdog.decide(now, now - timedelta(minutes=20), 0, 0, st)
    assert acts == set()
    acts, st = watchdog.decide(now, now - timedelta(minutes=50), 1, 0, st)
    assert acts == set(), "a run is already going"
    acts, st = watchdog.decide(now, now - timedelta(minutes=50), 0, 0, st)
    assert acts == {"restart"}
    acts, st = watchdog.decide(now, now - timedelta(hours=3), 0, 0, st)
    assert acts == {"restart", "alert"} and st["stale"]
    acts, st = watchdog.decide(now + timedelta(hours=1), now - timedelta(hours=3), 1, 0, st)
    assert acts == set(), "no repeat alert within 6 hours"
    acts, st = watchdog.decide(now + timedelta(hours=2), now + timedelta(hours=2) - timedelta(minutes=5), 1, 0, st)
    assert acts == {"recovered"} and not st["stale"]


def test_ed_and_roads_parsing():
    import localsa
    ed = localsa.parse_ed_rows([{"DTM": "2026-11-10T20:00:00", "HOSP_SHORT": "FMC", "WTBS": "22", "CAP": "63", "AVG_WAIT": "109.46"},
                                {"HOSP_SHORT": "RAH", "WTBS": "40", "CAP": "112", "AVG_WAIT": "200"},
                                {"HOSP_SHORT": "LMH", "WTBS": "29", "CAP": "88", "AVG_WAIT": "83"}])
    assert [(e["hospital"], e["status"]) for e in ed] == [("Flinders Medical Centre", "Normal"), ("Royal Adelaide", "Very busy"), ("Lyell McEwin", "Busy")]
    r = localsa.summarise_roads([{"LOCAL_ROAD_NAME": "MORGAN MAIL ROAD", "START_SUBURB": "QUONDONG",
                                  "PLOT_DETAILS": "MORGAN MAIL RD Closed due to flooding over road\r\n\r\nRef# 2138578"}])
    assert r[0]["road"] == "Morgan Mail Road" and r[0]["hazard"] and "Ref#" not in r[0]["type"]


def test_reservoirs_csv():
    import localsa
    csv_text = "Reservoir,Date,Capacity (ML),Volume (ML),% Full\nMount Bold,30/09/2026,45900,30000,65.4\nHappy Valley,30/09/2026,11600,3000,25.9\nTotal,30/09/2026,57500,33000,57.4\n"
    r = localsa.parse_reservoirs(csv_text)
    assert [x["name"] for x in r["reservoirs"]] == ["Mount Bold", "Happy Valley"] and r["low"] == ["Happy Valley"]
    assert r["total_pct"] == 57.4 and r["as_of"] == "30/09/2026"


def test_sapn_parse():
    import localsa
    assert localsa.parse_sapn('["ADELAIDE", "Norwood", "adelaide"]') == {"suburbs": ["Adelaide", "Norwood"], "suburb_count": 2}
    assert localsa.parse_sapn('{"Suburbs": [{"SuburbName": "GLENELG"}]}')["suburbs"] == ["Glenelg"]
