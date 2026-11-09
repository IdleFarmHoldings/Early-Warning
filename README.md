# Early Warning

A family early-warning system for threats that could reach South Australia: conflict, nuclear, cyber, pandemic, natural disasters, fuel and food supply, and market shocks.

## How it works

1. **Collector** (`collector/`, runs every 15 minutes on GitHub Actions, free)
   - Polls official feeds and public data, keeps a rolling history, and applies simple rules.
   - Pushes alerts straight to phones through [ntfy](https://ntfy.sh).
   - Writes `data/latest.json` (current picture) and `data/series/*.jsonl` (history).
2. **Analysis** (Claude, twice a day)
   - Reads `data/latest.json`, checks the news, scores each category, updates the Early Warning Board.
   - Writes any alert it decides on into `alerts/outbox/`; the collector pushes it on its next run.

## Alert channels

| Channel | Gets |
|---|---|
| Early warning (proactive) | Daily digest, unconfirmed early signals (unusual flight activity, market stress), official advisories and disaster alerts |
| Confirmed only (reactive) | Confirmed large-scale events affecting Australia: Red disaster alerts in Australia, WHO international health emergencies, and confirmed events the analysis flags |

Channel names are stored as repository secrets `NTFY_PROACTIVE` and `NTFY_REACTIVE`, never in the code.

## Sources

| Source | Used for |
|---|---|
| US State Dept travel advisories | Countries raised to Level 4, staff departure orders |
| UK FCDO travel advice | "Advise against all travel" / leave-now updates |
| GDACS (UN/EU) | Orange and Red disaster alerts |
| USGS | M7+ earthquakes in our region, tsunami flags |
| WHO Disease Outbreak News | Outbreak reports, international health emergencies |
| ASD's ACSC and US CISA | Cyber alerts and advisories |
| Yahoo Finance (unofficial) | VIX, gold, Brent oil, AUD/USD |
| [adsb.fi](https://adsb.fi) open data | Long-range private jet counts, retreat destinations, military, tanker and nuclear-command aircraft |

Aircraft data courtesy of [adsb.fi](https://adsb.fi), used for personal, non-commercial purposes.

Flight anomalies need about a week of history before they can alert ("learning baseline").

## Files

- `collector/config.json` — thresholds, watched aircraft types, hubs, keywords
- `state/state.json` — what has already been alerted (prevents repeats)
- `alerts/log.jsonl` — every alert sent
- `tests/` — offline tests: `python tests/test_collector.py`
