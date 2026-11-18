"""Cyber campaign tracker: join up small cyber incidents and read them as if an enemy is behind them.

Each check, cyber headlines from the news watch (and CISA alerts) are turned into reports with a date, the
countries, sectors and techniques they mention, and any named attacker. Reports are kept in a rolling ledger
(data/cyber_ledger.json) so incidents that each look small can be linked over weeks. Then:

  1. Reports about the same thing (same sector and country within 48 hours) are merged into one incident.
  2. Incidents are linked into suspected campaigns when they share a named attacker, or when they use the same
     technique against the same sector within two weeks. An unattributed incident that matches a named
     attacker's pattern joins that attacker's campaign as "linked by pattern".
  3. Each campaign is scored 0-100 for how much it looks like a deliberate campaign (named state attacker,
     how many countries and sectors, how damaging the technique, pace, official warnings, reaching Australia).
  4. Adversary lens: each campaign gets a hypothesis written as if the attack is deliberate - the likely aim,
     what it would hit next, what would confirm it, what it would mean for the family and what to do - from
     collector/cyber_playbook.json. The other explanations are always stated beside it.

Nothing here is a confirmed attribution and nothing here pushes a phone alert: the output feeds the family
page and the twice-daily analysis, which tests the hypotheses and decides on alerts.
Pure functions apart from the ledger list passed in, so they can be tested offline.
"""
import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

from common import iso

PB = json.loads((Path(__file__).resolve().parent / "cyber_playbook.json").read_text())
TOPICS = ["cyber_state", "cyber_ot", "cyber_attrib", "cyber_au", "cyber_supply", "ai_hack", "cloud", "bank_outage",
          "outage_au", "national_outage", "cables", "scams", "deepfake"]
FAMILY_ORDER = ["water", "food", "energy", "finance", "telecom", "fuel", "health", "transport", "it", "government"]
CRITICAL_SECTORS = {"water", "energy", "finance", "telecom", "fuel", "transport", "health", "food"}
ALLIES = set(PB["allies"])


def _rx(p, cs=False):
    return re.compile(p, 0 if cs else re.I)


COUNTRY_RX = {k: (_rx(v["re"]), _rx(v["cs"], cs=True) if v.get("cs") else None) for k, v in PB["countries"].items()}
SECTOR_RX = {k: _rx(v["re"]) for k, v in PB["sectors"].items()}
TECH_RX = {k: _rx(v["re"]) for k, v in PB["techniques"].items()}
ACTOR_RX = {k: _rx(v["re"]) for k, v in PB["actors"].items()}
OFFICIAL = _rx(PB["official_re"])
ATTRIBUTED = _rx(PB["attributed_re"])
NOT_EVENT = _rx(PB["not_event_re"])


def _clean(title):
    """Google News titles end in ' - Publisher'; drop it so a publisher's name never counts as a match."""
    t = (title or "").strip()
    return re.sub(r"\s+[-–|]\s+[^-–|]{2,60}$", "", t).strip(), (re.search(r"\s+[-–|]\s+([^-–|]{2,60})$", t) or [None, ""])[1]


def _when(raw, now):
    try:
        d = parsedate_to_datetime(raw)
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except Exception:  # noqa: BLE001
        try:
            return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except Exception:  # noqa: BLE001
            return now


def classify(title):
    """Countries, sectors, techniques (most damaging first) and named attacker in one headline, or None if it
    does not describe a cyber incident."""
    head, _ = _clean(title)
    if not head or NOT_EVENT.search(head):
        return None
    techs = [k for k in sorted(TECH_RX, key=lambda k: -PB["techniques"][k]["weight"]) if TECH_RX[k].search(head)]
    if not techs:
        return None
    sectors = [k for k, r in SECTOR_RX.items() if r.search(head)]
    attack = any(t != "outage" for t in techs)
    if not attack and not (set(sectors) & CRITICAL_SECTORS):
        return None  # an outage at something unimportant is not part of a campaign
    if techs != ["outage"] and "outage" in techs:
        techs.remove("outage")
    countries = [k for k, (r, cs) in COUNTRY_RX.items() if r.search(head) or (cs and cs.search(head))]
    actor = next((k for k, r in ACTOR_RX.items() if r.search(head)), None)
    if actor and PB["actors"][actor]["kind"] == "state" and not attack:
        actor = None  # "outage in China" is not a Chinese attack
    return {"kind": "attack" if attack else "outage", "countries": countries, "sectors": sectors or ["other"],
            "techniques": techs, "actor": actor, "official": bool(OFFICIAL.search(head)),
            "attributed": bool(actor and ATTRIBUTED.search(head) and OFFICIAL.search(head))}


def harvest(news, cisa, now):
    """New reports from this check's news watch and CISA alerts."""
    out = []
    for key in TOPICS:
        for it in ((news or {}).get(key) or {}).get("items", []):
            c = classify(it.get("title"))
            if c:
                head, pub = _clean(it["title"])
                out.append({**c, "title": head, "pub": pub, "link": it.get("link"), "at": iso(_when(it.get("date"), now)),
                            "topic": key})
    for a in cisa or []:
        link = a.get("link") or ""
        if "/ics-advisories/" in link:
            continue  # routine product bulletins, not attacks
        c = classify(a.get("title"))
        if c:
            c["official"] = True
            if "United States" not in c["countries"]:
                c["countries"].append("United States")
            out.append({**c, "title": a["title"], "pub": "CISA", "link": link, "at": iso(_when(a.get("date"), now)),
                        "topic": "cisa"})
    return out


def _rid(r):
    return hashlib.sha1(re.sub(r"\W+", " ", r["title"].lower()).strip().encode()).hexdigest()[:12]


def update_ledger(ledger, reports, now):
    """Add new reports (deduplicated by headline) and drop those older than the ledger window."""
    keep_from = now - timedelta(days=PB["ledger_days"])
    seen = {r["id"] for r in ledger}
    for r in reports:
        r = {**r, "id": _rid(r)}
        if r["id"] not in seen:
            ledger.append(r)
            seen.add(r["id"])
    ledger[:] = [r for r in ledger if _when(r["at"], now) >= keep_from]
    ledger.sort(key=lambda r: r["at"])
    return ledger


STOP = set("a an and the of in on at to for from by with as is are was were be has have after over into amid new says say said "
           "its it this that than more most".split())


def _words(t):
    return {w for w in re.findall(r"[a-z0-9']+", (t or "").lower()) if w not in STOP and len(w) > 2}


def _similar(a, b, cut=0.4):
    """Two headlines about the same story share most of their key words."""
    wa, wb = _words(a), _words(b)
    return bool(wa and wb) and len(wa & wb) / min(len(wa), len(wb)) >= cut


def incidents(ledger, now):
    """Merge reports about the same thing: same main sector and an overlapping country (or same named attacker)
    within incident_merge_hours."""
    gap = timedelta(hours=PB["incident_merge_hours"])
    out = []
    for r in sorted(ledger, key=lambda r: r["at"]):
        t = _when(r["at"], now)
        for inc in reversed(out):
            if t - inc["_last"] > gap:
                continue
            same_where = bool(set(r["countries"]) & set(inc["countries"]))
            same_how = bool(set(r["techniques"]) & set(inc["techniques"]) - {"outage", "intrusion"})
            same_story = _similar(r["title"], inc["reports"][0]["title"])
            if same_story or (r["sectors"][0] == inc["sectors"][0] and r["actor"] == inc["actor"]
                              and (same_where or (same_how and not (r["countries"] or inc["countries"])))):
                inc["reports"].append(r)
                inc["_last"] = max(inc["_last"], t)
                for k in ("countries", "sectors", "techniques"):
                    inc[k] += [x for x in r[k] if x not in inc[k]]
                if r["actor"] and inc["actor"] and r["actor"] != inc["actor"]:
                    pass  # keep the first named attacker; a second name in coverage is not a merge reason
                inc["actor"] = inc["actor"] or r["actor"]
                inc["kind"] = "attack" if "attack" in (inc["kind"], r["kind"]) else "outage"
                inc["official"] = inc["official"] or r["official"]
                inc["attributed"] = inc["attributed"] or r["attributed"]
                break
        else:
            out.append({"reports": [r], "_first": t, "_last": t, "countries": list(r["countries"]),
                        "sectors": list(r["sectors"]), "techniques": list(r["techniques"]), "actor": r["actor"],
                        "kind": r["kind"], "official": r["official"], "attributed": r["attributed"]})
    for i, inc in enumerate(out):
        inc["n"] = i
        inc["techniques"].sort(key=lambda k: -PB["techniques"][k]["weight"])
    return out


def _signature(inc):
    """(technique, sector) pairs that describe how and what was hit. Only distinctive techniques count: outages,
    vague break-ins, phishing and data theft are too common to link incidents on their own."""
    techs = [t for t in inc["techniques"] if PB["techniques"][t]["weight"] >= 8]  # phishing, data theft etc. are too common to link on
    sig = {(t, s) for t in techs for s in inc["sectors"] if s != "other"}
    # Tampering with equipment and destructive attacks are rare enough that the method alone links incidents
    return sig | {(t, "*") for t in techs if PB["techniques"][t]["weight"] >= 20}


def link(incs):
    """Union-find over incidents: same named attacker, or same technique against the same sector within link_days."""
    parent = list(range(len(incs)))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def join(a, b):
        parent[find(a)] = find(b)

    days = timedelta(days=PB["link_days"])
    for a in incs:
        for b in incs[:a["n"]]:
            if a["actor"] and a["actor"] == b["actor"]:
                join(a["n"], b["n"])
            elif abs(a["_first"] - b["_first"]) <= days and _signature(a) & _signature(b):
                if a["actor"] and b["actor"] and a["actor"] != b["actor"]:
                    continue  # two different named attackers stay apart
                join(a["n"], b["n"])
    groups = {}
    for inc in incs:
        groups.setdefault(find(inc["n"]), []).append(inc)
    return list(groups.values())


def _band(score):
    return "Strong" if score >= 70 else "Likely" if score >= 50 else "Possible" if score >= 30 else "Weak"


def score(group, now):
    """0-100: how much this looks like a deliberate campaign. Not a probability."""
    actors = [i["actor"] for i in group if i["actor"]]
    actor = max(set(actors), key=actors.count) if actors else None
    kind = PB["actors"][actor]["kind"] if actor else None
    countries = {c for i in group for c in i["countries"]}
    sectors = {s for i in group for s in i["sectors"]} - {"other"}
    techs = {t for i in group for t in i["techniques"]}
    attacks = [i for i in group if i["kind"] == "attack"]
    last7 = [i for i in group if now - i["_last"] <= timedelta(days=7)]
    prev7 = [i for i in group if timedelta(days=7) < now - i["_last"] <= timedelta(days=14)]
    s = {"state": 30, "hacktivist": 15, "criminal": 10}.get(kind, 0)
    s += min(18, 6 * len(countries & ALLIES))
    s += min(20, 5 * len(sectors))
    s += max([PB["techniques"][t]["weight"] for t in techs] or [0])
    s += 10 if len(last7) >= 3 else 5 if len(last7) >= 2 else 0
    s += 5 if len(last7) > len(prev7) else 0
    s += 10 if any(i["official"] for i in group) else 0
    s += 10 if "Australia" in countries or "New Zealand" in countries else 0
    if len(group) < 2 or not attacks:
        s = min(s, 25)  # one incident, or outages only, is not a campaign
    if len(group) < 3:
        s = min(s, 45)  # two incidents can be coincidence: at most "Possible"
    if not any(i["attributed"] for i in group):
        s = min(s, 85)  # without an official attribution it stays a hypothesis
    return min(100, s), actor, countries, sectors, techs, last7, prev7


def _names(keys, table):
    return [PB[table][k]["name"] for k in keys if k in PB[table]]


def _list(xs):
    xs = list(xs)
    return ", ".join(xs[:-1]) + (" and " if len(xs) > 1 else "") + xs[-1] if xs else ""


def hypothesis(actor, countries, sectors, techs, group, attributed):
    """The adversary-lens reading of a campaign, with the other explanations beside it."""
    p = PB["actors"].get(actor) if actor else PB["unknown"]
    tech_names = _names([t for t in sorted(techs, key=lambda t: -PB["techniques"][t]["weight"]) if t != "outage"][:3], "techniques")
    sec_names = _names(sorted(sectors), "sectors")
    days = max(1, round((max(i["_last"] for i in group) - min(i["_first"] for i in group)).total_seconds() / 86400))
    who = f"{actor}" if actor and PB["actors"][actor]["kind"] == "state" else (actor or "an unknown attacker")
    pattern = (f"{len(group)} incident{'s' if len(group) > 1 else ''} over {days} day{'s' if days > 1 else ''}"
               + (f": {_list(tech_names)}" if tech_names else "")
               + (f" against {_list(sec_names)}" if sec_names else "")
               + (f" in {_list(sorted(countries))}" if countries else "") + ".")
    nxt = list(p["next"])
    if actor:  # sectors this attacker usually goes after but has not hit yet in this campaign
        gaps = [PB["sectors"][s]["name"] for s in p.get("typical", []) if s not in sectors]
        if gaps:
            nxt.append(f"Not hit yet in this campaign but typical for {actor}: {'; '.join(gaps[:3])}")
    hit = [s for s in FAMILY_ORDER if s in sectors]  # what would matter most at home first
    for_us = [PB["sectors"][s]["for_us"] for s in hit][:3]
    acts, seen = [], set()
    for s in hit:
        a = PB["sectors"][s]["action"]
        if a["action"] not in seen:
            acts.append(a)
            seen.add(a["action"])
    other = ("A government has publicly linked some of these attacks to " + actor + ", but not every incident here is "
             "confirmed as part of it.") if attributed else (
        "These may not be connected: criminal gangs, faulty systems, or reporters linking separate events can look the "
        "same. No government has confirmed this as one campaign." if actor or len(group) > 1 else
        "This is a single incident so far; one incident is not a campaign.")
    return {"if_deliberate": f"If this is a deliberate campaign by {who}, the likely aim is: {p['motive']}",
            "pattern": pattern, "next_targets": nxt[:5], "confirm_if": p["escalation"][:4],
            "other_explanations": other, "australia": p["au_exposure"], "for_us": for_us, "actions": acts[:2]}


def build(ledger, now, prev=None):
    """Campaigns from the ledger, strongest first."""
    prev_scores = {c["key"]: c["score"] for c in (prev or {}).get("campaigns", [])}
    incs = incidents(ledger, now)
    out = []
    for group in link(incs):
        sc, actor, countries, sectors, techs, last7, prev7 = score(group, now)
        if sc < 30:
            continue
        sec_names = _names(sorted(sectors), "sectors")
        key = re.sub(r"\W+", "-", (actor or "unattributed-" + "-".join(sorted(sectors)[:2])).lower()).strip("-")
        title = (f"Suspected {actor} campaign" if actor and PB["actors"][actor]["kind"] == "state"
                 else f"{actor}: attacks on {_list(sec_names[:2])}" if actor
                 else f"Unattributed attacks on {_list(sec_names[:2]) or 'several targets'}")
        attributed = any(i["attributed"] for i in group)
        p = prev_scores.get(key)
        out.append({
            "key": key, "title": title, "actor": actor, "actor_kind": PB["actors"][actor]["kind"] if actor else None,
            "score": sc, "band": _band(sc),
            "trend": "up" if (p is not None and sc > p) or (p is None and len(last7) > len(prev7)) else
                     "down" if (p is not None and sc < p) else "steady",
            "first_seen": iso(min(i["_first"] for i in group)), "last_seen": iso(max(i["_last"] for i in group)),
            "countries": sorted(countries), "sectors": sec_names,
            "techniques": _names(sorted(techs, key=lambda t: -PB["techniques"][t]["weight"]), "techniques"),
            "incidents_7d": len(last7), "touches_australia": bool({"Australia", "New Zealand"} & countries),
            "official_warning": any(i["official"] for i in group), "attributed_officially": attributed,
            "incidents": [{"date": iso(i["_first"])[:10], "countries": i["countries"],
                           "sector": PB["sectors"].get(i["sectors"][0], {}).get("name", "other"),
                           "how": PB["techniques"][i["techniques"][0]]["name"], "kind": i["kind"],
                           "linked_by": "named attacker" if i["actor"] == actor and actor else "same pattern",
                           "reports": len(i["reports"]),
                           "title": i["reports"][0]["title"], "link": i["reports"][0].get("link")}
                          for i in sorted(group, key=lambda i: i["_first"], reverse=True)][:12],
            "hypothesis": hypothesis(actor, countries, sectors, techs, group, attributed)})
    out.sort(key=lambda c: -c["score"])
    return {"generated_at": iso(now), "campaigns": out[:8], "reports_in_ledger": len(ledger),
            "note": "Small cyber incidents linked into suspected campaigns and read as if deliberate. Scores (0-100) "
                    "show how much a cluster looks like a deliberate campaign, not a probability or a confirmed finding."}
