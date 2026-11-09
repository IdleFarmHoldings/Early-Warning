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

| [IODA](https://ioda.inetintel.cc.gatech.edu) (Georgia Tech) | Telstra, Optus, TPG, Vodafone and Aussie Broadband reachability: a sudden drop triggers a "you may lose signal" alert |
| adsb.fi navigation integrity (NIC) | GPS jamming: share of aircraft with degraded GPS over the Baltic, Black Sea, Israel, Gulf, Taiwan Strait, Korea, SA and NSW |
| [NGA maritime safety warnings](https://msi.nga.mil) | Missile, rocket and live-fire warnings around Taiwan and the China seas, Korea, the Gulf and Red Sea, and Australia |
| IODA country signals | Internet blackouts in Iran, Israel, Taiwan, Russia, China and South Korea |
| Google News search (last 24 h) | Reservist call-ups, mobilisation orders, cancelled military leave, ADF reserve call-outs, airspace closures (for the analysis run only) |
| Yahoo Finance sector baskets | Pandemic-response (APT, LAKE, MRNA, NVAX, EBS, SIGA) and defence (US, Europe, Australia) stocks vs their market over 5 days |
| Google News: health & signalling | Mass casualty / Code Brown declarations, medical export bans, Australian medicine shortages, unknown illness clusters, military blood drives and field hospitals, Russian and Chinese war signalling |
| CISA Known Exploited Vulnerabilities | Newly exploited flaws per week vs normal, especially VPN / firewall (edge) devices |
| SANS Internet Storm Center | Global threat level (Infocon), scanning of industrial-control and remote-access ports, top attacking addresses (geolocated by ip-api.com) |
| abuse.ch Feodo Tracker | Botnet command servers by country |
| ransomware.live | Ransomware victims by country (7 days) and Australian victims vs normal |
| Human Mortality Database (STMF) | Weekly deaths vs normal for ~38 countries (lags several weeks) |
| aisstream.io live AIS (free key) | Vessels loitering over undersea-cable corridors around Australia, Taiwan, the Baltic and Red Sea |
| Crisis Group, ASPI Strategist, Lowy Interpreter | Analyst headlines for the analysis run (context only, never pushed on their own) |

Aircraft data courtesy of [adsb.fi](https://adsb.fi), used for personal, non-commercial purposes.

Military flights: counts are kept per region (Middle East & Gulf, Taiwan & South China Sea, Korea & Japan, Eastern Europe & Black Sea, Australia & PNG). Three unconfirmed signals can alert: a region suddenly going quiet, an unusual number of aircraft vanishing from tracking at altitude ("going dark"), and surges in military transport or RAAF flights (possible evacuations or deployments). Many military flights never broadcast at all, and tracking gaps cause false alarms, so these are always marked UNCONFIRMED.

Precursor playbook (`collector/playbook.json`): seven scenarios, each with weighted warning signs grouped into domains (military air, maritime, space, diplomatic, markets, health, trade...). Signs from three or more separate domains at once lift a scenario to Elevated, and every scenario carries a plain explanation of how its signs are converging. Real events recorded by the analysis are journaled with the signs that preceded them (`data/events.jsonl`, `data/patterns.json`).

Flight anomalies need about a week of history before they can alert ("learning baseline").

## Files

- `collector/config.json` — thresholds, watched aircraft types, hubs, keywords
- `state/state.json` — what has already been alerted (prevents repeats)
- `alerts/log.jsonl` — every alert sent
- `tests/` — offline tests: `python tests/test_collector.py`

## Ship tracking key (one-off setup)

1. Sign in at https://aisstream.io with GitHub and copy your free API key.
2. In this repository: Settings > Secrets and variables > Actions > New repository secret. Name: `AISSTREAM_KEY`, value: the key.

Until the key is added, the "Undersea cables" section shows a setup note and everything else runs normally.

## Runs without your computer

Everything runs in the cloud: the collector on GitHub Actions every 15 minutes, the twice-daily analysis as a scheduled Claude task, alerts through ntfy.sh, and the family page on GitHub Pages. No laptop needs to be on or signed in.
