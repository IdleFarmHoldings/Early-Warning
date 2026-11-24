# Early Warning — notes for Claude

A family early-warning system for threats that could reach South Australia. Family members with no technical
background rely on the phone alerts and the family page, so **false alarms and silent failures both matter**.
This repo is public: never put the family's town, suburb, home coordinates or ntfy topic names in it.

## How it fits together

| Part | Where | What it does |
|---|---|---|
| Collector | `collector/run.py`, GitHub Actions `collector.yml` | Every 15 min: polls feeds, applies rules, pushes alerts to ntfy, writes data. Each run loops ~50 min and checks the outbox every minute, also during a full check (a watcher thread in `run.py`). |
| Watchdog | `collector/watchdog.py`, `watchdog.yml` | Every 30 min: restarts a stalled collector, alerts if data is over 2 h old. |
| Analysis | Scheduled Claude task "Early warning analysis" (04:39 and 16:39 Adelaide, Opus 5.5) | Reads `data/brief.json`, searches the news, updates the board and `data/status.json`, writes alerts to `alerts/outbox/`. |
| Scorecard | Scheduled Claude task "Early warning monthly scorecard" | Judges last month's alerts into `data/scorecard.json`. |
| Family page | `index.html` (+ `analytics.html`, `plan.html`) on GitHub Pages | Reads live data from the `live-data` branch via raw.githubusercontent.com. |
| Phone alerts | ntfy.sh, topics in secrets `NTFY_PROACTIVE` / `NTFY_REACTIVE` | Proactive = "Early warning" channel; reactive = "Confirmed only". |

The scheduled task prompts live in the user's Claude account, not in this repo. If you change a data path,
file format or alert rule they depend on, update the prompts too (or tell the user they need updating).

## Branches

- `main`: code, pages, tests. Nothing automated commits here.
- `live-data`: `data/`, `state/`, `alerts/`, written by the collector, the watchdog and the scheduled tasks.
  Workflows check it out into `_data/` and set `EW_DATA=_data`; `common.DATA_ROOT` resolves every data path
  from that. Never commit data files to main, and never rewrite `live-data` history while the collector runs.
- `data/demo-*.json` stay on main: they are page assets for `?demo=` simulations.

## Working on it

- Tests: `python -m pytest -q tests` (offline, ~2 s). Run them before every push.
- To look at live data locally: `git fetch origin live-data && git worktree add _data live-data`, then
  `EW_DATA=_data python ...`.
- The user's choice: push tested changes **straight to main**, no pull requests. The running collector pulls
  main every minute, so a push is live within ~1 minute (workflow file changes apply from the next run).
- Commit messages: say what changed for the family, not just the code.

## Alert rules the family has chosen

- Plain language for non-technical readers: short sentences, no jargon, say what it means and what to do.
- Every alert should pass: "would the family do or decide something differently because of this?"
- Warning-sign alerts must show the evidence behind each sign (the headline or reading), not just a count.
- Single-sensor signs (GPS jamming, military flight counts, vessels near cables) are held on a watch list and
  only pushed when 2+ kinds line up in one region, or GPS jamming persists over SA / NSW.
- Cyber/outage triage weighs proximity, spread and criticality (core internet > banks/payments > major platform
  > obscure app). Critical (both channels, priority 5) needs evidence Australians are hit: a headline naming
  Australia or an Australian company, or our own telemetry. An Australian outlet running an overseas story is
  not enough. A Microsoft-type platform outage is a Warning.
- News alone never sets off the emergency alarm (priority 5, both channels, repeats). Critical also needs something
  measured or first-hand to agree: our own telemetry (carrier drop, SA grid demand collapse) or a provider's status
  page. A news-only incident that would be Critical goes out once as a Warning saying so, and is marked
  `needs_confirming` in `brief.json` for the analysis. Official warnings (CFS, Bureau, WHO, GDACS) and measured
  signals keep their own emergency paths.
- Market moves are never pushed on their own: they are marked `big_move` for the board and the daily digest.
- Warning-sign alerts go out only when a sign the family has not been told about lights up (12 h cooldown); signs
  already told stay known while they show and for 72 h after (`state.signs_told`). The same signs dipping under
  the alert line and coming back do not re-alert.
- National shocks abroad (a whole country's flights, power, networks or payments halted) are alerted.
- CFS fires near home, including suburban house fires, must alert. Emergency-level alerts repeat twice.
  Ordinary CFS incidents push a phone note only under 10 km from home (`notify_radius_km`); farther ones (out to
  60 km, house fires to 25 km) show on the page and board only. Watch and Act and Emergency Warnings still alert
  out to 150 km.
- 4chan chatter (`collector/chan.py`, boards /pol/ /k/ /news/) is a tip-off for the analysis only: bursts of
  new threads on a topic go into `brief.json` as `chan_chatter`. It never pushes, never joins the watch list,
  and is never quoted to the family. Its baseline is learned per hour of day (~3 days to warm up).
- News signs ignore stories older than 72 h; keyword matches are noisy, so test new keywords against real
  headlines (see `tests/test_signs.py`) before relying on them.

## First-hand sources

- `collector/firsthand.py`: providers' own status pages (Google Cloud, AWS, Azure, Cloudflare). Only a BIG
  incident (the provider's top severity) joins incident triage, as one official report: it can push the usual
  one-off Warning, never Critical on its own. Small ones are listed in `brief.json` `status_pages`.
- UKMTO: probe only (`latest.json` `ukmto`, samples in `data/probe/` on live-data) until a parser is written.
- Downdetector is not used: no public API, and its terms forbid scraping.

## Gotchas

- Google News sometimes returns months-old stories even for "last 24 h" searches.
- WHO's unsorted fallback feed returns archive reports; only reports from the last 14 days may alert.
- ntfy accepting a message (HTTP 200) is recorded as sent; phone-side delivery can still fail.
- `state/state.json` remembers what was alerted; deleting keys there re-sends old alerts.
