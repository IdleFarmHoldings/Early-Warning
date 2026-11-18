import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "collector"))
import campaigns  # noqa: E402

NOW = datetime(2026, 11, 18, 0, 0, tzinfo=timezone.utc)


def rss(*items):
    return {"items": [{"title": t, "date": (NOW - timedelta(days=d)).strftime("%a, %d %b %Y %H:%M:%S GMT"), "link": f"https://x/{i}"}
                      for i, (t, d) in enumerate(items)]}


def test_classify_filters_noise():
    assert campaigns.classify("Labor backs down on backpacker visa cuts amid debate over rents - ABC") is None
    assert campaigns.classify("Is Iran Quietly Targeting America's Critical Infrastructure? - Substack") is None
    assert campaigns.classify("Germany Passes BND Hacking Law Without Zero-day Disclosure Rules") is None
    c = campaigns.classify("Iranian hackers lock operators out of control panels at US water stations - WSJ")
    assert c["actor"] == "Iran" and c["sectors"][0] == "water" and c["techniques"][0] == "physical"
    assert "United States" in c["countries"] and c["kind"] == "attack"
    assert campaigns.classify("Outage hits Chinese food delivery app")["actor"] is None  # an outage is not an attack


def test_small_attacks_link_into_one_campaign_and_noise_stays_out():
    news = {
        "cyber_ot": rss(("Iranian hackers lock operators out of control panels at US water stations - WSJ", 2),
                        ("Hackers raise temperatures on supermarket freezers across Europe - Reuters", 6),
                        ("Pentagon probes freezer temperatures changed by hackers at military bases - Reuters", 8),
                        ("Suspected Iranian hackers hit Texas-bound tanker networks - WSJ", 2)),
        "bank_outage": rss(("Chase Bank up and running after outage - CNN", 1)),
        "cyber_supply": rss(("Ransomware gang hits hospital chain in Ohio - AP", 3)),
    }
    ledger = campaigns.update_ledger([], campaigns.harvest(news, [], NOW), NOW)
    out = campaigns.build(ledger, NOW)
    top = out["campaigns"][0]
    assert top["actor"] == "Iran" and top["band"] in ("Likely", "Strong")
    titles = [i["title"] for i in top["incidents"]]
    assert any("freezers across Europe" in t for t in titles)  # unattributed, joined by the same pattern
    assert any(i["linked_by"] == "same pattern" for i in top["incidents"])
    assert not any("Chase" in t for c in out["campaigns"] for t in [i["title"] for i in c["incidents"]])
    h = top["hypothesis"]
    assert h["if_deliberate"].startswith("If this is a deliberate campaign by Iran")
    assert h["other_explanations"] and h["next_targets"] and h["actions"]
    assert h["actions"][0]["action"].startswith("Keep two days of bottled water")  # water matters most at home
    assert top["score"] <= 85  # no official attribution: stays a hypothesis


def test_two_incidents_are_at_most_possible_and_ledger_dedupes():
    news = {"cyber_state": rss(("Russian hackers deploy wiper malware at Polish energy company - Reuters", 1),
                               ("Russian hackers deploy wiper malware at Polish energy company - Reuters", 1),
                               ("Wiper attack hits Danish power utility - Bloomberg", 3))}
    ledger = campaigns.update_ledger([], campaigns.harvest(news, [], NOW), NOW)
    assert len(ledger) == 2
    out = campaigns.build(ledger, NOW)
    assert out["campaigns"] and out["campaigns"][0]["score"] <= 45 and out["campaigns"][0]["band"] == "Possible"
    old = [{**ledger[0], "id": "old", "at": (NOW - timedelta(days=60)).isoformat()}]
    assert campaigns.update_ledger(old, [], NOW) == []  # older than the ledger window


def test_reaching_australia_is_flagged():
    news = {"cyber_ot": rss(("Iranian hackers tamper with US water utility controllers - AP", 4),
                            ("Hackers tamper with water treatment controllers in Victoria - ABC", 1),
                            ("Iran-linked hackers change freezer temperatures in UK supermarkets - BBC", 2))}
    out = campaigns.build(campaigns.update_ledger([], campaigns.harvest(news, [], NOW), NOW), NOW)
    assert out["campaigns"][0]["touches_australia"] is True
