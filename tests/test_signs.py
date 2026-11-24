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
    out = run.precursor_alerts(scen, {"scenarios": [{"key": "bio", "score": 40}]}, {"signs_told": {}}, NOW, first_run=False)
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


def test_australian_bank_hack_in_the_news_is_a_warning_naming_australia():
    news_ = _feed("CommBank confirms cyber attack, customers locked out - Reuters",
                  "Westpac and NAB card payments down nationwide after hack - 9News",
                  "EFTPOS outage hits supermarkets across Australia - SMH",
                  "Banks confirm payments outage, thousands stranded at checkouts - The Guardian",
                  "ATMs down as bank hack spreads - news.com.au")
    state = {}
    s = {x["target"]: x for x in I.triage(news_, NOW, state)}["Banking & payments"]
    # Australians are hit, but only news says so: a Warning saying so, not the repeating emergency alarm (24 Nov)
    assert s["severity"] == "Warning" and s["au_proof"] is True and s["news_only_critical"] is True
    out = I.incident_alerts([s], state, NOW, first_run=False)
    assert out[0]["channels"] == ["proactive"] and out[0]["priority"] == 4 and "in Australia" in out[0]["title"]


def test_market_moves_are_marked_for_the_digest_not_pushed():
    """21-23 Nov 2026: Friday's VIX jump went out as 'Market stress' on Saturday, Sunday and Monday."""
    state = {"bootstrapped": True, "primed": ["markets"]}
    for day in range(3):
        mk = {"VIX": {"price": 35.89, "change_pct": 26.2, "prev_close": 28.44},
              "Gold": {"price": 3661.0, "change_pct": 4.1, "prev_close": 3516.8},
              "AUD/USD": {"price": 0.66, "change_pct": -0.2, "prev_close": 0.661}}
        alerts, state, _ = evaluate({"markets": mk}, state, [], NOW + timedelta(days=day))
        assert not [a for a in alerts if a["category"] == "markets"]
        assert mk["VIX"]["big_move"] and mk["Gold"]["big_move"] and not mk["AUD/USD"].get("big_move")


def test_pandemic_signs_ignore_the_misfires_of_19_to_23_november():
    masscas, chem = ind("bio", "masscas"), ind("bio", "chem")
    for t in ["Family of Robb Elementary victim hope their experience inspires mass casualty preparedness at hospitals - KSAT",
              "A DECADE LATER: How traffic investigators preserved 'mass casualty' evidence between grieving parents - WTOP",
              "Portsmouth accident declared a mass casualty incident - BBC"]:
        assert P._lit(masscas, news("masscas", t)) is None, t
    assert P._lit(masscas, news("masscas", "Dozens Fall Ill On Two Condor Flights To Frankfurt, Triggering Mass Casualty Response - Simple Flying"))
    for t in ["Warning of Suspected Chemical Weapons Storage at Sudanese Army Facilities - Sudan Tribune",
              "Russian colonel suspected of ordering use of chemical weapons against Ukrainian troops - Kyiv Independent",
              "Meeting Between Iran's Deputy FM and Chemical Weapons Victims - IFP News"]:
        assert P._lit(chem, news("chem", t)) is None, t
    assert P._lit(chem, news("chem", "Panic at 12,000-strong concert as suspected chemical attack sparks mass evacuation - Daily Mail"))
    assert "vix" not in {i["id"] for s in P.PLAYBOOK["scenarios"] if s["key"] == "bio" for i in s["indicators"]}


def _scen(score, *ids):
    lit = [{"id": i, "name": i, "short": i.title(), "detail": f"{i} headline", "weight": 20} for i in ids]
    return [{"key": "bio", "name": "Pandemic, biological or chemical emergency", "score": score, "lit": lit}]


def test_same_signs_coming_back_do_not_alert_again():
    """19-23 Nov 2026: the pandemic alert went out five times on much the same signs."""
    state, t, sent = {"signs_told": {}}, NOW, []
    for score, ids in [(65, ("who", "illness", "chem")), (55, ("who", "illness")), (65, ("who", "illness", "chem")),
                       (40, ("who",)), (65, ("who", "illness", "chem"))]:
        sent += run.precursor_alerts(_scen(score, *ids), {}, state, t, first_run=False)
        t += timedelta(hours=14)  # past the 12 h cooldown each time
    assert len(sent) == 1
    # a sign the family has not heard about does alert, listed first and marked NEW
    out = run.precursor_alerts(_scen(70, "who", "illness", "chem", "dieoff"), {}, state, t, first_run=False)
    assert len(out) == 1 and out[0]["message"].split("\n")[1].startswith("• NEW Dieoff")
    # and once a sign has been gone for more than 3 days it counts as new again
    t += timedelta(days=4)
    run.precursor_alerts(_scen(30), {}, state, t, first_run=False)
    assert run.precursor_alerts(_scen(65, "who", "illness", "chem"), {}, state, t + timedelta(hours=1), first_run=False)


def test_signs_already_showing_when_this_rule_starts_are_not_pushed():
    state = {}
    assert run.precursor_alerts(_scen(65, "who", "illness"), {}, state, NOW, first_run=False) == []
    assert run.precursor_alerts(_scen(65, "who", "illness"), {}, state, NOW + timedelta(hours=13), first_run=False) == []
