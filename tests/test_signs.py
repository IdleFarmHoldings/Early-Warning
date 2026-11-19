"""Warning-sign alerts (19-20 Nov 2026): the bio alert said '4 signs' without saying what they were, and three of
the four were junk (a July news story, a political row about chemical-attack reports, two 'Bigfoot hunters').
The cyber alert called a US card-data hack 'cyber attack in Australia' because an Australian paper ran it."""
import sys
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "collector"))

import incidents as I  # noqa: E402
import precursors as P  # noqa: E402
import run  # noqa: E402
from rules import evaluate  # noqa: E402

NOW = datetime(2026, 11, 19, 21, 1, tzinfo=timezone.utc)


def ind(scenario, iid):
    return next(i for s in P.PLAYBOOK["scenarios"] if s["key"] == scenario for i in s["indicators"] if i["id"] == iid)


def news(key, title, hours_ago=2):
    return {"now": NOW, "news": {key: {"items": [{"title": title, "date": format_datetime(NOW - timedelta(hours=hours_ago))}]}}}


def test_old_news_is_not_a_warning_sign():
    title = "What to know about the severe pneumonia cluster on NYC's Upper East Side - NBC News"
    assert P._lit(ind("bio", "illness"), news("illness", title, hours_ago=2))
    assert P._lit(ind("bio", "illness"), news("illness", title, hours_ago=24 * 135)) is None  # the July story


def test_talk_about_chemical_attacks_is_not_an_attack():
    chem = ind("bio", "chem")
    for t in ["Marco Rubio lashes out at CNN over chemical attack reports - News24",
              "Iranian Foreign Ministry spokesman calls chemical weapons allegations 'fraudulent' - The Times of Israel",
              "Has Iran handed the Houthis Assad's chemical weapons playbook? - Arab News",
              "OPCW fosters young scientists committed to chemical safety and the peaceful uses of chemistry - OPCW"]:
        assert P._lit(chem, news("chem", t)) is None, t
    for t in ["Suspected chemical attack in Syrian town kills dozens - Reuters",
              "Nerve agent attack leaves 12 hospitalised in Tbilisi - Reuters",
              "Chlorine gas attack: victims treated in hospital - BBC"]:
        assert P._lit(chem, news("chem", t)), t


def test_die_off_needs_animals_or_many_people():
    d = ind("bio", "dieoff")
    assert P._lit(d, news("dieoff", "Chuck Thompson on the mysterious deaths of two Bigfoot hunters in Washington - Crime Reads")) is None
    assert P._lit(d, news("dieoff", "Six most haunted castles in Scotland with spooky tales - ScottishNews.com")) is None
    assert P._lit(d, news("dieoff", "Mass die-off of birds along the Coorong puzzles scientists - ABC"))
    assert P._lit(d, news("dieoff", "Unexplained deaths of dozens of villagers in Congo prompt WHO team - Reuters"))


def test_signs_alert_lists_what_set_each_sign_off():
    s = {"key": "bio", "name": "Pandemic, biological or chemical emergency",
         "lit": [{"id": "who", "name": "WHO outbreak report with a high-concern pathogen (last 14 days)",
                  "short": "WHO outbreak report", "detail": "Middle East respiratory syndrome (MERS) - Qatar", "weight": 15},
                 {"id": "illness", "name": "Unknown illness / pneumonia cluster reported", "short": "Illness cluster",
                  "detail": "Mystery illness outbreak in Caldas da Rainha grows to 122 cases", "weight": 20},
                 {"id": "vix", "name": "Market fear index above 30", "detail": "VIX at 31.2", "weight": 10}]}
    m = run.signs_message(s)
    assert m.startswith("3 automatic warning signs showing")
    assert "• Illness cluster: Mystery illness outbreak in Caldas da Rainha" in m
    assert "• WHO outbreak report: Middle East respiratory syndrome" in m
    assert "• Market fear index above 30: VIX at 31.2" in m  # no short label: falls back to the name
    assert m.index("Illness cluster") < m.index("WHO outbreak")  # heaviest sign first
    assert len(m) <= 700


def test_signs_alert_stays_short_with_many_signs():
    lit = [{"id": f"x{n}", "name": f"Sign number {n}", "detail": "A long headline " * 8, "weight": 10} for n in range(9)]
    m = run.signs_message({"key": "bio", "name": "x", "lit": lit})
    assert len(m) <= 700 and "more on the board" in m and m.endswith("will check them.")


def test_precursor_alert_uses_the_right_category_and_detail():
    lit = [{"id": "who", "name": "WHO report", "short": "WHO outbreak report", "detail": "Ebola - DRC", "weight": 60}]
    scen = [{"key": "bio", "name": "Pandemic, biological or chemical emergency", "score": 60, "lit": lit}]
    out = run.precursor_alerts(scen, {"scenarios": [{"key": "bio", "score": 40}]}, {}, NOW, first_run=False)
    assert len(out) == 1 and out[0]["category"] == "bio" and "Ebola - DRC" in out[0]["message"]
    assert out[0]["channels"] == ["proactive"]


def test_archive_who_reports_never_alert():
    old = {"id": "who-1997", "title": "1997 - Ebola haemorrhagic fever in Gabon", "summary": "Ebola", "date": "1997-01-09T00:00:00Z", "url": "u"}
    fresh = {"id": "who-2026", "title": "Ebola disease - Democratic Republic of the Congo", "summary": "Ebola",
             "date": "2026-11-18T09:00:00Z", "url": "u"}
    alerts, state, _ = evaluate({"who": [old, fresh]}, {"bootstrapped": True, "primed": ["who"]}, [], NOW)
    titles = [a["title"] for a in alerts if a["category"] == "bio"]
    assert any("Congo" in t for t in titles) and not any("1997" in t for t in titles)


def _feed(*titles):
    return {"cyber_supply": {"items": [{"title": t, "date": format_datetime(NOW - timedelta(minutes=20 * i + 5)), "link": "x"}
                                       for i, t in enumerate(titles)]}}


def test_overseas_hack_in_an_australian_paper_is_not_critical():
    news_ = _feed("Hackers steal data from Visa, MasterCard holders - Herald Sun",
                  "JPMorgan, Visa, Mastercard say third-party vendors targeted in cyberattack - Reuters",
                  "Visa, Mastercard and Chase bank customer data hacked after string of major failures - New York Post",
                  "Mastercard confirms hackers breached a payments supplier - CNBC",
                  "Card networks hit by hack on vendors, banks say - Bloomberg")
    state = {}
    s = {x["target"]: x for x in I.triage(news_, NOW, state)}["Banking & payments"]
    assert s["severity"] == "Warning" and s["au_proof"] is False
    out = I.incident_alerts([s], state, NOW, first_run=False)
    assert out[0]["channels"] == ["proactive"] and out[0]["priority"] == 4
    assert "in Australia" not in out[0]["title"] and "Not yet clear whether Australians are affected" in out[0]["message"]


def test_australian_bank_hack_is_still_critical():
    news_ = _feed("CommBank confirms cyber attack, customers locked out - Reuters",
                  "Westpac and NAB card payments down nationwide after hack - 9News",
                  "EFTPOS outage hits supermarkets across Australia - SMH",
                  "Banks confirm payments outage, thousands stranded at checkouts - The Guardian",
                  "ATMs down as bank hack spreads - news.com.au")
    state = {}
    s = {x["target"]: x for x in I.triage(news_, NOW, state)}["Banking & payments"]
    assert s["severity"] == "Critical" and s["au_proof"] is True
    out = I.incident_alerts([s], state, NOW, first_run=False)
    assert out[0]["channels"] == ["proactive", "reactive"] and "in Australia" in out[0]["title"]
