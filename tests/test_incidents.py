import sys
from datetime import timedelta
from email.utils import format_datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "collector"))

from common import now_utc  # noqa: E402
import incidents as I  # noqa: E402

NOW = now_utc().replace(second=0, microsecond=0)


def item(title, mins_ago=30):
    return {"title": title, "date": format_datetime(NOW - timedelta(minutes=mins_ago)), "link": "https://x/" + title[:10]}


def feed(*titles, key="cloud"):
    return {key: {"items": [item(t, 20 * i + 5) for i, t in enumerate(titles)]}}


def by_target(scored):
    return {s["target"]: s for s in scored}


def test_microsoft_outage_in_sa_is_warning():
    news = feed("Microsoft Outlook outage disrupts email services across Australia - Glam Adelaide",
                "Microsoft crash hits hospitals and spy agency - Adelaide Now",
                "SA Govt update as Microsoft outage disrupts ASIO - InDaily South Australia",
                "Microsoft outage recovery 'not progressing as expected' - Michael West Media",
                "A global Microsoft 365 outage has hit thousands - The Guardian")
    s = by_target(I.triage(news, NOW, {}))["Microsoft"]
    assert (s["proximity"], s["criticality"], s["severity"]) == (3, 2, "Warning")
    assert "Proximity" in s["rationale"] and "Velocity" in s["rationale"] and "Criticality" in s["rationale"]


def test_bank_outage_spreading_in_australia_is_critical():
    news = feed("CommBank outage leaves Australians unable to pay - ABC News",
                "Westpac and NAB customers report card payments down nationwide - 9News",
                "EFTPOS outage hits supermarkets and airports across Australia - SMH",
                "Banks confirm payments outage, thousands stranded at checkouts - The Guardian",
                "ATMs down as bank outage spreads - news.com.au", key="bank_outage")
    s = by_target(I.triage(news, NOW, {}))["Banking & payments"]
    assert s["severity"] == "Critical" and s["criticality"] == 3 and s["velocity"] == 3


def test_obscure_app_overseas_is_info():
    news = feed("Little-known note app Zappy down for some users in Germany - TechBlog")
    s = I.triage(news, NOW, {})[0]
    assert s["severity"] == "Info" and s["proximity"] == 0


def test_single_unconfirmed_report_never_pushes():
    news = feed("Optus outage? Some users say mobile network down - Reddit roundup")
    s = I.triage(news, NOW, {})[0]
    assert s["criticality"] == 4 and s["severity"] in ("Info", "Warning")
    assert s["confirmed"] is False and s["severity"] == "Info"


def test_carrier_telemetry_counts_as_core_incident():
    tele = {"Telstra": {"bgp": {"drop_pct": 0}, "ping-slash24": {"drop_pct": 35}}}
    s = by_target(I.triage({}, NOW, {}, telecom=tele))["Phone & internet networks"]
    assert s["criticality"] == 4 and s["confirmed"]


def test_old_and_non_live_headlines_are_ignored():
    news = {"cloud": {"items": [item("Microsoft outage hits Australia - ABC", mins_ago=60 * 30),
                                item("Cloud outage market size to exceed $5 billion - PR Wire")]}}
    assert I.triage(news, NOW, {}) == []


def test_alerts_once_then_again_only_when_severity_rises():
    state = {}
    warn = feed("Microsoft outage hits Adelaide hospitals - Adelaide Now", "Microsoft 365 down across Australia - ABC")
    a1 = I.incident_alerts(I.triage(warn, NOW, state), state, NOW, first_run=False)
    assert len(a1) == 1 and a1[0]["channels"] == ["proactive"] and a1[0]["priority"] == 4
    a2 = I.incident_alerts(I.triage(warn, NOW + timedelta(minutes=15), state), state, NOW + timedelta(minutes=15), False)
    assert a2 == []
    # Same target escalates to Critical (simulated) -> re-alert on both channels
    sc = I.triage(warn, NOW + timedelta(minutes=30), state)
    sc[0]["severity"] = "Critical"
    a3 = I.incident_alerts(sc, state, NOW + timedelta(minutes=30), False)
    assert len(a3) == 1 and set(a3[0]["channels"]) == {"proactive", "reactive"} and a3[0]["priority"] == 5


def test_first_run_is_silent():
    state = {}
    warn = feed("Microsoft outage hits Adelaide hospitals - Adelaide Now", "Microsoft 365 down across Australia - ABC")
    assert I.incident_alerts(I.triage(warn, NOW, state), state, NOW, first_run=True) == []


def test_immigration_visas_and_statistics_are_not_bank_outages():
    assert I.classify("Global Microsoft outage: New Zealand suspends visa processing")[1] == "Microsoft"
    assert I.classify("Immigration Online: Microsoft outage blocks visa applications")[1] == "Microsoft"
    assert I.classify("Visa card payments down across Australia")[1] == "Banking & payments"
    assert I.classify("NAB app down for thousands")[1] == "Banking & payments"
    assert I.classify("Snab Ltd shares fall")[1] is None
    news = feed("UK student visa applications down 31% as Russell Group warns of volatility - The PIE News")
    assert I.triage(news, NOW, {}) == []
