"""SA blackout check vs rooftop solar: replays the false 'possible large power outage' alerts of 12 Nov 2026."""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "collector"))

import localsa  # noqa: E402

# (UTC time, MW at alert, MW ~30 min before (reconstructed from the alert's % fall), usual MW) - all daytime in Adelaide
FALSE_ALERTS = [
    ((2026, 11, 11, 22, 36), 650, 931, 1298), ((2026, 11, 11, 23, 27), 366, 615, 1247),
    ((2026, 11, 12, 0, 3), 265, 379, 1251), ((2026, 11, 12, 1, 31), 149, 277, 1072),
    ((2026, 11, 12, 2, 7), 66, 148, 1036), ((2026, 11, 12, 3, 14), 115, 195, 636)]


def _hist(now, before, usual):
    ts = lambda m: (now - timedelta(minutes=m)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return [[ts(30), before]] + [[ts(1440 * d), usual] for d in (1, 2, 3)]


def test_solar_dips_no_longer_alert():
    for when, mw, before, usual in FALSE_ALERTS:
        now = datetime(*when, tzinfo=timezone.utc)
        hist = _hist(now, before, usual)
        # the old hourly rule (what the collector did before) flags every one of them
        assert localsa.demand_status(now, mw, hist, solar_sun_deg=999)["possible_blackout"] is True, when
        for kw in ({"prev10_mw": mw + 60}, {"prev10_mw": mw + 60, "pv_mw": 1400}, {"prev10_mw": mw + 150, "pv_mw": 1400}):
            s = localsa.demand_status(now, mw, hist, **kw)
            assert s["solar_active"] and s["possible_blackout"] is False, (when, kw)


def test_real_midday_blackout_still_alerts():
    for when, _mw, before, usual in FALSE_ALERTS:
        now = datetime(*when, tzinfo=timezone.utc)
        s = localsa.demand_status(now, 40, _hist(now, 1000, usual), prev10_mw=900, pv_mw=1400)
        assert s["possible_blackout"] is True and s["fall_mw_10min"] == 860, when


def test_night_rule_unchanged():
    now = datetime(2026, 11, 10, 14, 0, tzinfo=timezone.utc)  # 00:30 in Adelaide
    hist = _hist(now, 1400, 1450)
    assert localsa.demand_status(now, 300, hist, prev10_mw=1350)["possible_blackout"] is True
    assert localsa.demand_status(now, 1300, hist, prev10_mw=1350)["possible_blackout"] is False


def test_solar_detection():
    noon = datetime(2026, 11, 12, 2, 30, tzinfo=timezone.utc)   # 13:00 Adelaide
    night = datetime(2026, 11, 12, 13, 30, tzinfo=timezone.utc)  # 00:00 Adelaide
    assert localsa.sun_elevation(noon) > 60 and localsa.sun_elevation(night) < -20
    assert localsa.demand_status(noon, 900, [])["solar_active"] is True
    assert localsa.demand_status(night, 900, [])["solar_active"] is False
    # AEMO's rooftop estimate also counts (e.g. just after sunrise when the sun is still low)
    assert localsa.demand_status(night, 900, [], pv_mw=500)["solar_active"] is True


def test_parse_rooftop_pv():
    csv_text = ('C,NEMP.WORLD,ROOFTOP_PV_ACTUAL_MEASUREMENT\n'
                'I,ROOFTOP,ACTUAL,2,INTERVAL_DATETIME,REGIONID,POWER,QI,TYPE,LASTCHANGED\n'
                'D,ROOFTOP,ACTUAL,2,"2026/11/12 12:00:00",SA1,1350.2,1,MEASUREMENT,"2026/11/12 12:31:00"\n'
                'D,ROOFTOP,ACTUAL,2,"2026/11/12 12:30:00",SA1,1402.7,1,MEASUREMENT,"2026/11/12 13:01:00"\n'
                'D,ROOFTOP,ACTUAL,2,"2026/11/12 12:30:00",VIC1,3000,1,MEASUREMENT,"2026/11/12 13:01:00"\n')
    assert localsa.parse_rooftop_pv(csv_text) == ("2026/11/12 12:30:00", 1402.7)
