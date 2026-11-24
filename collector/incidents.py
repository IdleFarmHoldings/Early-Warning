"""Incident triage: weigh three converging signals for outages and cyber incidents.

  1. Proximity   - how close the impact is to the family (SA > Australia > global > elsewhere)
  2. Velocity    - how fast reports are spreading and how many sectors are hit
  3. Criticality - how systemically important the target is

  Criticality tiers (highest first):
    4 core      - internet backbone, phone networks, undersea cables, DNS/CDN, power grid
    3 finance   - banks, card payments / EFTPOS, ATMs, the share market
    2 platform  - Microsoft / AWS / Google / Apple / CrowdStrike and similar platforms
    1 app       - any other, low-dependency service

  Severity:
    Critical - high proximity (Australia/SA) + rapid spread + high criticality (tier 3+), AND something measured or
               first-hand agrees (our own telemetry or a provider's status page). News alone is never an emergency.
    Warning  - high spread + material criticality (tier 2+), OR confirmed impact in SA. A news-only incident that
               would otherwise be Critical is a Warning marked news_only_critical, for the analysis to confirm.
    Info     - anything else (dashboard only, never pushed)

Pure functions apart from the `state` dict, so they can be tested offline.
"""
import re
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

from common import CONFIG, iso

CFG = CONFIG.get("incidents", {})

# Feeds whose headlines can describe an outage or cyber incident.
FEEDS = CFG.get("feeds", ["cloud", "cyber_au", "cyber_supply", "sapn", "cables", "ai_hack", "outage_au", "bank_outage"])
WINDOW_H = CFG.get("window_hours", 12)

IMPACT = re.compile(
    r"\b(outages?|down(?!\s*(by\s*)?\d)|offline|disrupt\w*|crash(es|ed)?|meltdown|fail(s|ed|ure)?|not working|blackouts?|"
    r"cyber[- ]?attack\w*|hack(ed|ers?)?|breach(ed)?|ransomware|ddos|severed|cut)\b", re.I)
# Headlines that use the words but do not describe a live incident.
NOT_LIVE = re.compile(
    r"\b(market size|forecast to|webinar|how to|lessons from|anniversary|years? (ago|after)|report finds|"
    r"named a leader|contest|award|fined|sentenced|court|inquiry into last|"
    # Talk about risk or politics, not a live outage (23 Nov 2026: "Report shows ageing coal plants boost
    # blackout risk" and "SA Health denies using blackout as smokescreen" went out as "Power grid: outage in SA").
    r"(blackout|outage|grid|cyber) risks?|risks? of (a )?(blackouts?|outages?)|reports? (shows?|finds?|warns?)|"
    r"study (shows?|finds?)|not reliable|denies|denied|smokescreen|backflip|plans? for)\b", re.I)

# Aftermath coverage: the incident is over or being picked over. These never start or repeat an alert.
AFTERMATH = re.compile(
    r"\b(restor(es|ed|ing)|fully restored|back online|resolved|recover(s|y|ed|ing)|resum(es|ed|ing)|"
    r"(deeply )?sorry|apologi[sz]e[sd]?|rules? out|ruled out|explain(s|ed|er)?|explanation|post[- ]?mortem|review|"
    r"what (to know|we know|happened|went wrong)|what .{2,30} need to know|lessons?|aftermath|in the wake of|"
    r"after (the|a|an|last|this|yesterday'?s?) |following (the|a|last) |insurers?|insurance|payouts?|class[- ]action|"
    r"compensation|loss estimates?|cost of|costs? (businesses|firms|the economy)|questions|blame[sd]?|"
    r"should .{2,40} be worried|analysis|opinion|shows? (the|how|why)|how (a|the) .{3,60} (rocked|unfolded|happened)|"
    r"warns? (another|of another|it could happen again)|could happen again)\b", re.I)

# Each target: (tier, label, regex). The first match at the highest tier wins.
TARGETS = [
    (4, "Phone & internet networks", r"\b(Telstra|Optus|TPG|Vodafone|NBN|triple zero|000 calls?|mobile network|internet outage|nationwide outage)\b"),
    (4, "Undersea cables", r"\b(undersea|subsea|submarine) cables?\b"),
    (4, "Internet core (DNS / CDN)", r"\b(Cloudflare|Akamai|DNS|BGP|internet backbone)\b"),
    (4, "Power grid", r"\b(power grid|statewide blackout|SA Power Networks|AEMO|without power|blackouts?)\b"),
    (3, "Banking & payments", r"\b(Commonwealth Bank|CommBank|Westpac|Bendigo Bank|Macquarie Bank|banks?|banking|EFTPOS|eftpos|"
                              r"card payments?|payment systems?|Mastercard|Osko|PayID|Tyro|PayPal|"
                              r"(?-i:CBA|NAB|ANZ|ING|ATMs?|NPP|ASX)|(?-i:Visa)(?! (applications?|processing|holders?|rules?|system for)))\b"),
    (3, "Supermarkets & food", r"\b(Coles|Woolworths|Woolies|Aldi|IGA|Foodland|Drakes|Metcash|supermarkets?)\b"),
    (3, "Fuel supply", r"\b(Ampol|Viva Energy|BP Australia|Caltex|Mobil|United Petroleum|fuel (terminals?|pumps?|supply|distribution)|"
                       r"petrol stations?|servos?)\b"),
    (3, "Health & hospitals", r"\b(SA Health|hospitals?|Medicare|pharmac(y|ies)|ambulance|My Health Record|pathology)\b"),
    (3, "Government services", r"\b(myGov|Centrelink|Services Australia|ATO|Service SA|SA Government|SA Police)\b"),
    (2, "Microsoft", r"\b(Microsoft( 365)?|Office 365|Outlook|Teams|Azure|Windows|Entra)\b"),
    (2, "Amazon Web Services", r"\b(AWS|Amazon Web Services)\b"),
    (2, "Google", r"\b(Google Cloud|Gmail|Google Workspace|Android)\b"),
    (2, "Apple", r"\b(iCloud|Apple Pay|iOS|Apple)\b"),
    (2, "CrowdStrike", r"\bCrowdStrike\b"),
    (2, "Meta", r"\b(Meta|Facebook|WhatsApp|Instagram)\b"),
    (2, "AI platforms", r"\b(OpenAI|ChatGPT|Anthropic|Claude)\b"),
    (2, "Identity & SaaS", r"\b(Okta|Salesforce|Oracle|SAP|ServiceNow|Starlink)\b"),
]
AU_ONLY = {"Supermarkets & food", "Fuel supply", "Government services"}  # Australian brands: always local reach
ATTACK = re.compile(r"\b(cyber[- ]?(attack|incident|security incident)\w*|hack(ed|ers?)?|breach(ed)?|ransomware|ddos)\b", re.I)
TIER_NAMES = {4: "core infrastructure", 3: "essential service", 2: "major platform", 1: "low-dependency app"}

SA = re.compile(r"\b(South Australia\w*|Adelaide|SA (Govt|Government|Health|Police|Power)|SA\b(?! \d))")
AU = re.compile(r"\b(Australia\w*|Aussies?|nationwide|ASIO|ATO|Centrelink|myGov|Medicare|NSW|Victoria\w*|Queensland\w*|"
                r"Sydney|Melbourne|Brisbane|Perth|Canberra|Tasmania\w*|ACCC|ACSC|ASD)\b", re.I)
GLOBAL = re.compile(r"\b(global|worldwide|world-wide|international|across the world|millions)\b", re.I)
# Publishers that are Australian (or SA) - a report there implies Australian users are hit.
AU_PUBS = re.compile(r"(ABC|Adelaide Now|InDaily|Nine|news\.com\.au|SMH|Sydney Morning Herald|The Age|Guardian Australia|"
                     r"iTnews|Information Age|AFR|Financial Review|The Australian|7NEWS|9News|SBS|Crikey|Michael West|"
                     r"Glam Adelaide|The Nightly|Daily Telegraph|Herald Sun|Courier-Mail|Canberra Times|AAP)", re.I)
SA_PUBS = re.compile(r"(Adelaide Now|InDaily|Glam Adelaide|Advertiser)", re.I)
# Australian companies: a headline naming one is about Australian customers, wherever it is published.
AU_BRANDS = re.compile(r"\b(Commonwealth Bank|CommBank|Westpac|Bendigo Bank|Macquarie Bank|(?-i:CBA|NAB|ANZ)|EFTPOS|"
                       r"Osko|PayID|Tyro|Telstra|Optus|TPG|NBN|triple zero|Aussie Broadband|Medibank|Qantas|"
                       r"Virgin Australia|Jetstar|Australia Post)\b", re.I)
# Signs an incident is spreading across society, not staying inside one product.
SPREAD = re.compile(r"\b(hospitals?|health|government|agenc(y|ies)|spy agency|ASIO|banks?|airports?|airlines?|flights?|"
                    r"emergency services|police|schools?|universit(y|ies)|retailers?|supermarkets?|thousands|millions|"
                    r"nationwide|worsening|not progressing|second day|hours-long|days-long|spreads?|widespread)\b", re.I)
CONFIRM = re.compile(r"\b(confirms?|confirmed|says|said|statement|acknowledg\w+|investigating|restor\w+|apologi[sz]\w+)\b", re.I)


def _split(item):
    t = item.get("title", "")
    return (t.rsplit(" - ", 1) + [""])[:2] if " - " in t else [t, ""]


def _when(item):
    try:
        dt = parsedate_to_datetime(item.get("date") or "")
        return dt if dt and dt.tzinfo else None
    except (TypeError, ValueError):
        return None


SUBJECT = r"(?:\W+[\w'-]+){0,2}?\W+(?:outages?|down\b|offline|disrupt\w*|crash\w*|meltdown|fail\w*|hack\w*|breach\w*|cyber[- ]?attack)"


def _subject(head):
    """A label for a headline about an organisation we don't track: its leading name ("Workday", "Optimus Bank")."""
    m = re.match(r"\W*((?:[A-Z][\w&'-]+)(?:\s+[A-Z][\w&'-]+){0,2})", head)
    name = m.group(1) if m else ""
    name = re.sub(r"\s+(Outage|Hack|Breach|Cyber\w*|Attack|Investigat\w+|Confirms?|Says|Hit|Down)\b.*$", "", name).strip()
    return name[:40] if len(name) >= 3 else "Other services"


def classify(head):
    """(tier, label) for the target a headline is about, or (1, None).
    A target named right before the outage word ("Microsoft outage blocks visa applications") wins; otherwise the
    most critical target named anywhere (TARGETS is ordered by tier)."""
    for tier, label, rx in TARGETS:
        if re.search(rx + SUBJECT, head, re.I):
            return tier, label
    for tier, label, rx in TARGETS:
        if re.search(rx, head, re.I):
            return tier, label
    return 1, None


def collect(news, now, telecom=None, local=None, status=None):
    """Group recent incident headlines by target. Returns {label: cluster}.
    status: active incidents from the providers' own status pages (firsthand.fetch_status()["active"]);
    only BIG ones join, as one official report each."""
    clusters = {}
    seen = set()
    for key in FEEDS:
        for it in (news or {}).get(key, {}).get("items", []):
            head, pub = _split(it)
            dt = _when(it)
            if not dt or now - dt > timedelta(hours=WINDOW_H) or dt - now > timedelta(hours=1):
                continue
            if not IMPACT.search(head) or NOT_LIVE.search(head):
                continue
            norm = re.sub(r"\W+", " ", head.lower()).strip()
            if norm in seen:
                continue
            seen.add(norm)
            tier, label = classify(head)
            label = label or _subject(head)
            c = clusters.setdefault(label, {"label": label, "tier": tier, "reports": [], "pubs": set(),
                                            "first": dt, "last": dt})
            c["tier"] = max(c["tier"], tier)
            c["first"], c["last"] = min(c["first"], dt), max(c["last"], dt)
            c["pubs"].add(pub.strip().lower())
            c["reports"].append({"title": head[:160], "pub": pub.strip(), "at": iso(dt), "link": it.get("link", ""),
                                 "sa": bool(SA.search(head)),
                                 "au": bool(AU.search(head) or AU_PUBS.search(pub) or SA_PUBS.search(pub)
                                            or (tier == 3 and label in AU_ONLY)),
                                 # The headline itself says Australians are hit (not just an Australian outlet
                                 # running a wire story about somewhere else).
                                 "au_head": bool(SA.search(head) or AU.search(head) or AU_BRANDS.search(head)
                                                 or (tier == 3 and label in AU_ONLY)),
                                 "after": bool(AFTERMATH.search(head)),
                                 "attack": bool(ATTACK.search(head)),
                                 "global": bool(GLOBAL.search(head)),
                                 "spread": sorted({m.group(0).lower() for m in SPREAD.finditer(head)}),
                                 "official": bool(CONFIRM.search(head))})
    # Telemetry: an Australian carrier measurably down is a confirmed core-infrastructure incident.
    for name, n in (telecom or {}).items():
        drops = [(n.get(s) or {}).get("drop_pct") or 0 for s in ("bgp", "ping-slash24")]
        if max(drops) >= CFG.get("carrier_drop_pct", 20):
            c = clusters.setdefault("Phone & internet networks", {"label": "Phone & internet networks", "tier": 4,
                                                                  "reports": [], "pubs": set(), "first": now, "last": now})
            c["reports"].append({"title": f"{name} network measurably down {max(drops):.0f}%", "pub": "telemetry",
                                 "at": iso(now), "link": "", "sa": False, "au": True, "au_head": True, "global": False,
                                 "spread": [], "official": True})
            c["pubs"].add("telemetry")
    # The provider's own status page: first-hand and official, usually before the news.
    for inc in status or []:
        if not inc.get("big"):
            continue
        tier, label = classify(f"{inc['provider']} {inc['title']}")
        if not label:
            tier, label = 2, inc["provider"]
        c = clusters.setdefault(label, {"label": label, "tier": tier, "reports": [], "pubs": set(),
                                        "first": now, "last": now})
        c["tier"] = max(c["tier"], tier)
        pub = f"{inc['provider']} status page"
        c["reports"].append({"title": f"{pub}: {inc['title']}"[:160], "pub": pub, "at": inc.get("started") or iso(now),
                             "link": inc.get("url", ""), "sa": False, "au": inc.get("au", False),
                             "au_head": inc.get("au", False), "global": not inc.get("au", False), "after": False,
                             "attack": bool(ATTACK.search(inc["title"])), "spread": [], "official": True,
                             "status_page": True})
        c["pubs"].add(pub.lower())
    if (local or {}).get("possible_blackout"):
        c = clusters.setdefault("Power grid", {"label": "Power grid", "tier": 4, "reports": [], "pubs": set(),
                                               "first": now, "last": now})
        c["reports"].append({"title": "Sudden large fall in SA electricity demand", "pub": "telemetry", "at": iso(now),
                             "link": "", "sa": True, "au": True, "au_head": True, "global": False, "spread": [],
                             "official": False})
        c["pubs"].add("telemetry")
    return clusters


def score(c, prev=None, now=None):
    """Score one cluster on the three signals and decide severity."""
    allreps = c["reports"]
    reps = [r for r in allreps if not r.get("after")] or []
    winding_down = len(reps) < max(1, len(allreps) - len(reps))  # mostly "restored / what we learned" coverage now
    if not reps:
        reps = allreps
    n_pubs = len({r["pub"].strip().lower() for r in reps if r["pub"]})
    # 1. Proximity: 3 SA, 2 Australia, 1 global (reaches Australian users), 0 elsewhere
    prox = 3 if any(r["sa"] for r in reps) else 2 if any(r["au"] for r in reps) else 1 if any(r["global"] for r in reps) else 0
    # 2. Velocity: reports and publishers in the window, growth since last run, and sectors hit
    sectors = sorted({s for r in reps for s in r["spread"]})
    growth = len(reps) - (prev or {}).get("reports", 0)
    hours = max((c["last"] - c["first"]).total_seconds() / 3600, 0.5)
    rate = len(reps) / hours
    vel = 1
    if len(reps) >= 3 or n_pubs >= 3 or len(sectors) >= 2 or growth >= 2:
        vel = 2
    if (len(reps) >= 5 and n_pubs >= 4) or len(sectors) >= 4 or (growth >= 3 and rate >= 1):
        vel = 3
    # A provider declaring its own top-severity incident counts as spreading: it has many customers by definition.
    if any(r.get("status_page") for r in reps):
        vel = max(vel, 2)
    # 3. Criticality: tier of the target
    crit = c["tier"]
    confirmed = n_pubs >= 2 or any(r["official"] for r in reps)
    # Critical goes to the Confirmed-only channel, so it needs a headline (or our own telemetry) that says
    # Australians are affected. An Australian paper carrying an overseas story is not enough on its own
    # (19 Nov 2026: a US card-data hack in the Herald Sun went out as "cyber attack in Australia").
    au_proof = any(r.get("sa") or r.get("au_head") for r in reps)
    # Something measured or first-hand, not a headline: our own telemetry (carrier drop, grid demand collapse) or
    # the provider's own status page. Headlines alone set off two false emergencies in five days (19 Nov: an
    # overseas card hack; 23 Nov: a report on blackout risk), each sent three times to both channels at priority 5.
    measured = any(r["pub"] == "telemetry" or r.get("status_page") for r in reps)

    if prox >= 2 and vel >= 3 and crit >= 3 and confirmed and au_proof:
        sev = "Critical"
    elif prox == 3 and confirmed and crit >= 3 and (len(reps) >= 3 or n_pubs >= 3 or measured):
        # In SA, but still needs 3+ reports or outlets (or our own telemetry): two stray headlines are not an emergency.
        sev = "Critical"
    elif (vel >= 2 and crit >= 2 and confirmed) or (prox == 3 and confirmed):
        sev = "Warning"
    else:
        sev = "Info"
    # The loud, repeating emergency alarm needs a measured or first-hand signal to agree with the news. Without
    # one it is a Warning (one buzz, Early warning channel); it becomes Critical as soon as telemetry or a status
    # page agrees, and the analysis can confirm it to the Confirmed-only channel.
    news_only_critical = sev == "Critical" and not measured
    if news_only_critical:
        sev = "Warning"
    if winding_down:
        sev = "Info"  # over, or being picked over: board only
        news_only_critical = False
    prox_txt = ["outside Australia", "global (reaches Australian users)", "Australia", "South Australia"][prox]
    if prox == 2 and not au_proof:
        prox_txt = "reported in Australian media, no sign yet of Australians affected"
    vel_txt = ["", "slow (isolated reports)", "spreading", "rapid / widespread"][vel]
    why = (f"Proximity: {prox_txt}. Velocity: {vel_txt} - {len(reps)} reports from {n_pubs} outlets"
           + (f", hitting {', '.join(sectors[:4])}" if sectors else "") + f". Criticality: {TIER_NAMES[crit]} ({c['label']}).")
    return {"key": re.sub(r"\W+", "-", c["label"].lower()).strip("-"), "target": c["label"], "severity": sev,
            "proximity": prox, "velocity": vel, "criticality": crit, "confirmed": confirmed,
            "reports": len(reps), "outlets": n_pubs, "sectors": sectors, "first_seen": iso(c["first"]), "au_proof": au_proof,
            "winding_down": winding_down, "news_only_critical": news_only_critical, "measured": measured,
            "attack": sum(1 for r in reps if r.get("attack")) * 2 >= len(reps),
            "rationale": why, "headlines": [{"title": r["title"], "pub": r["pub"], "at": r["at"], "link": r["link"]}
                                            for r in sorted(reps, key=lambda r: (not r.get("after"), r["at"]), reverse=True)[:4]]}


RANK = {"Info": 0, "Warning": 1, "Critical": 2}


def triage(news, now, state, telecom=None, local=None, status=None):
    """Score every current incident. Updates state['incidents'] for velocity tracking."""
    mem = state.setdefault("incidents", {})
    out = []
    for label, c in collect(news, now, telecom, local, status).items():
        s = score(c, mem.get(label), now)
        m = mem.setdefault(label, {"first_seen": s["first_seen"]})
        m.update({"reports": s["reports"], "last_seen": iso(now), "severity": s["severity"]})
        out.append(s)
    # Forget incidents with no reports for 2 days
    for label in [k for k, v in mem.items() if v.get("last_seen", "") < iso(now - timedelta(days=2))]:
        mem.pop(label)
    return sorted(out, key=lambda s: (-RANK[s["severity"]], -s["criticality"], -s["proximity"], -s["velocity"]))


def incident_alerts(scored, state, now, first_run):
    """Push Warning (Early warning channel) and Critical (both channels) incidents ONCE per incident, and again
    only if the severity rises. An incident counts as the same one until it has had no live reports for 2 days,
    so "services restored" and "what went wrong" stories never repeat the alert."""
    cd = state.setdefault("cooldown", {})
    out = []
    for s in scored:
        if s["severity"] == "Info":
            continue
        key = f"inc-{s['key']}"
        last = cd.get(key)  # "<iso>|<severity>|<last live report seen>"
        if last:
            parts = last.split("|")
            at = parts[0]
            sev = parts[1] if len(parts) > 1 and parts[1] else "Info"
            live = parts[2] if len(parts) > 2 else ""
            if not live:
                # Saved before the "last live report" field existed. That alert already went out, so carry on
                # as the same incident (re-alert only if it gets worse) rather than re-sending it now.
                same = True
            else:
                try:
                    same = now - datetime.strptime(live, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc) < timedelta(days=2)
                except ValueError:
                    same = True  # an unreadable record must never stop the other incidents being checked
            if same:
                cd[key] = f"{at}|{sev}|{iso(now)}"
                if RANK[s["severity"]] <= RANK.get(sev, 0):
                    continue
        cd[key] = f"{iso(now)}|{s['severity']}|{iso(now)}"
        if first_run:
            continue
        crit = s["severity"] == "Critical"
        top = s["headlines"][0]["title"] if s["headlines"] else ""
        where = {3: "in SA", 2: "in Australia", 1: "worldwide"}.get(s["proximity"], "")
        unclear = s["proximity"] == 2 and not s.get("au_proof", True)
        if unclear:
            where = ""  # only an Australian outlet carrying it: don't claim it is happening here
        what = "cyber attack" if s.get("attack") else "outage"
        advice = ("Be wary of emails or texts about it, and keep some cash handy in case payments are affected."
                  if s.get("attack") else "Use cash and phone calls as backup." if s["criticality"] >= 3
                  else "Use phone or SMS for anything urgent.")
        firsthand = [h for h in s["headlines"] if h["pub"].endswith("status page")]
        if firsthand and s["reports"] == len(firsthand):
            # Only the provider's own status page so far: say so plainly instead of "1 reports from 1 outlets".
            h = firsthand[0]
            out.append({"key": key, "channels": ["proactive", "reactive"] if crit else ["proactive"], "category": "cyber",
                        "title": f"{s['target']}: {what} {where}".strip()[:90],
                        "message": (f"{h['title']}. The company has declared a major incident on its own status page, "
                                    "so websites and apps that rely on it may not work for a while. "
                                    "No sign yet that it is an attack. Check the board."
                                    if not s.get("attack") else
                                    f"{h['title']}. {advice} Check the board.")[:390],
                        "priority": 5 if crit else 4, "confirmed": True,
                        "tags": ["rotating_light" if crit else "warning"]})
            continue
        out.append({"key": key, "channels": ["proactive", "reactive"] if crit else ["proactive"], "category": "cyber",
                    "title": f"{s['target']}: {what} {where}".strip()[:90],
                    "message": (f"{top}. {s['reports']} reports from {s['outlets']} outlets"
                                + (f"; also affecting {', '.join(s['sectors'][:3])}" if s["sectors"] else "")
                                + (". Not yet clear whether Australians are affected" if unclear else "")
                                + (". News reports only so far; our own checks have not confirmed it"
                                   if s.get("news_only_critical") else "")
                                + f". {advice} Check the board.")[:390],
                    "priority": 5 if crit else 4, "confirmed": s["confirmed"],
                    "tags": ["rotating_light" if crit else "warning"]})
    return out
