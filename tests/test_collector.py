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
