"""Gulf strikes (Nov 2026): the Houthis hit Riyadh's tallest tower on 19 and 24 Nov (11 hurt the second time) and
the Aramco tower on 8 Nov. The news searches only looked for attacks on US bases and on ships, so none of these
were caught, and every digest said "no new attacks on US bases". The gulf_strikes search covers missiles and drones
hitting Gulf cities and oil sites. It feeds the analysis brief only: it never pushes an alert on its own."""
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "collector"))

import brief  # noqa: E402

CONFIG = json.loads((Path(__file__).resolve().parent.parent / "collector" / "config.json").read_text())


def query(key):
    return next(w["q"] for w in CONFIG["news_watch"] if w["key"] == key)


def matches(q, title):
    """Headline-only stand-in for Google News: every (A OR B OR "c d") group must have a term in the title.
    Google also matches article text, so it finds more than this; a headline match is the minimum."""
    for group in re.findall(r"\(([^()]*)\)", q):
        terms = [t.strip().strip('"') for t in re.findall(r'"[^"]+"|[^\s()]+', group) if t != "OR"]
        if not any(re.search(r"\b" + re.escape(t) + r"\b", title, re.I) for t in terms):
            return False
    return True


REAL = [  # real headlines, 8-25 Nov 2026
    "Riyadh's tallest skyscraper hit in Houthi strike on Saudi wealth fund HQ - Reuters",
    "Houthi missile damages Riyadh tower, no injuries reported - Crypto Briefing",
    "Saudi Arabia says Houthi attack damaged Riyadh tower - Al Jazeera",
    "Eleven hurt in second Houthi strike within days on Riyadh's tallest tower - Reuters",
    "11 injured in Houthi attack on Riyadh's financial district tower: Saudi Civil Defense - Anadolu Ajansı",
    "Houthi Attack on Riyadh Tower Injures 11 People, Saudis Say - Bloomberg",
    "Houthi Forces Hit Saudi Aramco's Headquarters Building - The Maritime Executive",
    "Yemen's Houthis claim attack on Riyadh financial district, Aramco skyscraper damaged - Al-Monitor",
]

NOT_ATTACKS = [
    "Saudi Aramco posts lower quarterly profit as oil prices slide - Reuters",
    "Riyadh Season 2026 opens with record crowds - Arab News",
    "Dubai property prices hit another record high - Gulf News",
    "Qatar Airways adds more flights between Doha and Adelaide - Executive Traveller",
    "Iran attacks US air base in Kuwait - The Hill",  # the strikes search already covers this
]


def test_gulf_city_strikes_are_caught():
    for t in REAL:
        assert matches(query("gulf_strikes"), t), t


def test_the_old_searches_missed_them():
    old = [query("strikes"), query("shipping_attacks")]
    for t in REAL:
        assert not any(matches(q, t) for q in old), t


def test_ordinary_gulf_news_is_not_caught():
    for t in NOT_ATTACKS:
        assert not matches(query("gulf_strikes"), t), t


def test_attack_topics_get_five_headlines_in_the_brief():
    items = [{"title": f"Headline {i}", "date": "Tue, 24 Nov 2026 16:48:00 GMT"} for i in range(10)]
    b = brief.build({"news_watch": {"gulf_strikes": {"items": items}, "strikes": {"items": items},
                                    "airspace": {"items": items}}}, {})
    assert len(b["news_watch"]["gulf_strikes"]) == 5
    assert len(b["news_watch"]["strikes"]) == 5
    assert len(b["news_watch"]["airspace"]) == 3


def test_gulf_strikes_never_feed_the_alert_rules():
    """Digest and board only: incidents, campaigns, shocks and warning signs read their own topic lists."""
    import campaigns
    import incidents
    import precursors
    import shocks
    assert "gulf_strikes" not in incidents.FEEDS
    assert "gulf_strikes" not in campaigns.TOPICS
    assert "gulf_strikes" not in shocks.CFG.get("news_keys", [])
    assert "gulf_strikes" not in json.dumps(precursors.PLAYBOOK)
