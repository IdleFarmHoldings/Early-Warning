import sys
from datetime import timedelta
from email.utils import format_datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "collector"))

from common import iso, now_utc  # noqa: E402
import firsthand as F  # noqa: E402
import incidents as I  # noqa: E402

NOW = now_utc().replace(second=0, microsecond=0)


def rfc(mins_ago):
    return format_datetime(NOW - timedelta(minutes=mins_ago))


def test_statuspage_only_critical_is_big():
    data = {"incidents": [
        {"name": "Widespread 500 errors", "status": "investigating", "impact": "critical", "created_at": iso(NOW),
         "shortlink": "https://stspg.io/x", "components": [{"name": "CDN/Cache"}]},
        {"name": "Dashboard slow", "status": "identified", "impact": "major", "created_at": iso(NOW), "components": []},
        {"name": "Old one", "status": "resolved", "impact": "critical", "created_at": iso(NOW)}]}
    out = F.parse_statuspage(data, "Cloudflare", "https://www.cloudflarestatus.com")
    assert [(i["title"], i["big"]) for i in out] == [("Widespread 500 errors", True), ("Dashboard slow", False)]


def test_statuspage_sydney_component_marks_australia():
    data = {"incidents": [{"name": "Network issues", "status": "investigating", "impact": "minor",
                           "created_at": iso(NOW), "components": [{"name": "Sydney, Australia - (SYD)"}]}]}
    assert F.parse_statuspage(data, "Cloudflare", "")[0]["au"] is True


def test_gcp_active_high_and_wide_is_big_ended_or_narrow_is_not():
    data = [
        {"begin": iso(NOW - timedelta(hours=1)), "end": None, "severity": "high", "external_desc": "Multiple services down",
         "uri": "incidents/abc", "affected_products": [{"title": "A"}, {"title": "B"}, {"title": "C"}],
         "currently_affected_locations": [{"title": "Sydney (australia-southeast1)"}]},
        {"begin": iso(NOW - timedelta(hours=2)), "severity": "high", "external_desc": "One product",
         "affected_products": [{"title": "Vertex AI"}], "currently_affected_locations": [{"title": "Iowa (us-central1)"}]},
        {"begin": iso(NOW - timedelta(hours=3)), "end": iso(NOW), "severity": "high", "external_desc": "Fixed"},
        {"begin": iso(NOW - timedelta(days=9)), "severity": "high", "external_desc": "Ancient, never closed"},
    ]
    out = F.parse_gcp(data, NOW)
    assert [(i["title"], i["big"], i["au"]) for i in out] == [
        ("Google Cloud: Multiple services down", True, True), ("Google Cloud: One product", False, False)]
    assert out[0]["url"] == "https://status.cloud.google.com/incidents/abc"


def aws_rss(*items):
    body = "".join(f"<item><title>{t}</title><pubDate>{rfc(m)}</pubDate><guid>https://status.aws.amazon.com/#{g}</guid></item>"
                   for t, m, g in items)
    return f"<rss><channel>{body}</channel></rss>".encode()


def test_aws_newest_update_decides_and_disruption_is_big():
    raw = aws_rss(("Service is operating normally: [RESOLVED] Increased API Error Rates", 10, "ec2-us-east-1_1"),
                  ("Informational message: Increased API Error Rates", 60, "ec2-us-east-1_2"),
                  ("Service disruption: Connectivity issues", 20, "ec2-ap-southeast-2_3"),
                  ("Informational message: Delayed metrics", 30, "cloudwatch-eu-west-1_4"),
                  ("Service disruption: Way back", 600, "s3-us-east-1_5"))
    out = {i["where"]: i for i in F.parse_aws(raw, NOW)}
    assert set(out) == {"ec2-ap-southeast-2", "cloudwatch-eu-west-1"}
    assert out["ec2-ap-southeast-2"]["big"] and out["ec2-ap-southeast-2"]["au"]
    assert not out["cloudwatch-eu-west-1"]["big"]


def test_azure_recent_is_big_unless_mitigated():
    raw = (f"<rss><channel><item><title>Azure Front Door - connectivity issues</title><pubDate>{rfc(30)}</pubDate>"
           f"<link>https://azure.status.microsoft/x</link></item>"
           f"<item><title>Mitigated - Storage in East US</title><pubDate>{rfc(40)}</pubDate></item>"
           f"<item><title>Old</title><pubDate>{rfc(900)}</pubDate></item></channel></rss>").encode()
    out = F.parse_azure(raw, NOW)
    assert [(i["title"], i["big"]) for i in out] == [("Microsoft Azure: Azure Front Door - connectivity issues", True),
                                                      ("Microsoft Azure: Mitigated - Storage in East US", False)]


def test_big_status_incident_alone_pushes_one_warning_in_plain_words():
    status = [F._inc("Cloudflare", "Widespread 500 errors across the network", True, NOW, "https://stspg.io/x")]
    state = {}
    scored = I.triage({}, NOW, state, status=status)
    s = {x["target"]: x for x in scored}["Internet core (DNS / CDN)"]
    assert s["severity"] == "Warning" and s["confirmed"]
    alerts = I.incident_alerts(scored, state, NOW, first_run=False)
    assert len(alerts) == 1 and alerts[0]["channels"] == ["proactive"]
    assert "own status page" in alerts[0]["message"] and "reports from" not in alerts[0]["message"]
    # Same incident next check: no repeat
    assert I.incident_alerts(I.triage({}, NOW + timedelta(minutes=15), state, status=status), state,
                             NOW + timedelta(minutes=15), first_run=False) == []


def test_small_status_incident_stays_off_phones():
    status = [F._inc("Cloudflare", "Dashboard slow", False, NOW, "")]
    state = {}
    scored = I.triage({}, NOW, state, status=status)
    assert scored == [] and I.incident_alerts(scored, state, NOW, first_run=False) == []


def test_status_page_never_makes_critical_without_news():
    status = [F._inc("AWS", "AWS: Connectivity issues", True, NOW, "", where="ec2-ap-southeast-2", au=True)]
    s = I.triage({}, NOW, {}, status=status)[0]
    assert s["target"] == "Amazon Web Services" and s["severity"] == "Warning"
