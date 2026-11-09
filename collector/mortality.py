"""Weekly all-cause deaths vs normal per country (Human Mortality Database, Short-Term Mortality Fluctuations).
Official statistics lag by several weeks; they confirm unusual death waves rather than warn of them."""
import csv
import io
from datetime import date, datetime, timedelta, timezone

from common import http_get, iso

STMF = "https://www.mortality.org/File/GetDocument/Public/STMF/Outputs/stmf.csv"
CODES = {"AUS": ("AU", "Australia"), "AUT": ("AT", "Austria"), "BEL": ("BE", "Belgium"), "BGR": ("BG", "Bulgaria"),
         "CAN": ("CA", "Canada"), "CHE": ("CH", "Switzerland"), "CHL": ("CL", "Chile"), "CZE": ("CZ", "Czechia"),
         "DEUTNP": ("DE", "Germany"), "DNK": ("DK", "Denmark"), "ESP": ("ES", "Spain"), "EST": ("EE", "Estonia"),
         "FIN": ("FI", "Finland"), "FRATNP": ("FR", "France"), "GBRTENW": ("GB", "England & Wales"),
         "GBR_SCO": ("GB-SCT", "Scotland"), "GBR_NIR": ("GB-NIR", "Northern Ireland"), "GRC": ("GR", "Greece"),
         "HRV": ("HR", "Croatia"), "HUN": ("HU", "Hungary"), "ISL": ("IS", "Iceland"), "ISR": ("IL", "Israel"),
         "ITA": ("IT", "Italy"), "KOR": ("KR", "South Korea"), "LTU": ("LT", "Lithuania"), "LUX": ("LU", "Luxembourg"),
         "LVA": ("LV", "Latvia"), "NLD": ("NL", "Netherlands"), "NOR": ("NO", "Norway"), "NZL_NP": ("NZ", "New Zealand"),
         "POL": ("PL", "Poland"), "PRT": ("PT", "Portugal"), "RUS": ("RU", "Russia"), "SVK": ("SK", "Slovakia"),
         "SVN": ("SI", "Slovenia"), "SWE": ("SE", "Sweden"), "TWN": ("TW", "Taiwan"), "USA": ("US", "United States"),
         "JPN": ("JP", "Japan")}


def parse_stmf(text, now, base_years=3, history_weeks=12):
    lines = [l.lstrip("\ufeff").replace('"', "") for l in text.splitlines()]
    start = next((i for i, l in enumerate(lines[:50]) if "countrycode" in l.lower()), None)
    if start is None:
        raise ValueError("STMF header not found; starts: " + " | ".join(lines[:3])[:200])
    lines[start] = ",".join(h.strip() for h in lines[start].split(","))
    deaths = {}
    for r in csv.DictReader(io.StringIO("\n".join(lines[start:]))):
        if (r.get("Sex") or "").lower() != "b" or str(r.get("Forecast", "0")).strip() == "1":
            continue
        try:
            deaths[(r["CountryCode"], int(r["Year"]), int(r["Week"]))] = float(r["DTotal"])
        except (KeyError, ValueError):
            continue
    out = {}
    for code in {k[0] for k in deaths}:
        weeks = sorted((y, w) for c, y, w in deaths if c == code)
        series = []
        for y, w in weeks[-history_weeks:]:
            base = [deaths[(code, y - k, w)] for k in range(1, base_years + 1) if (code, y - k, w) in deaths]
            if len(base) < 2:
                continue
            b = sum(base) / len(base)
            series.append({"year": y, "week": w, "deaths": deaths[(code, y, w)], "expected": round(b),
                           "excess_pct": round((deaths[(code, y, w)] - b) / b * 100, 1) if b else None})
        if not series:
            continue
        last = series[-1]
        try:
            wk = date.fromisocalendar(last["year"], last["week"], 7)
            age = (now.date() - wk).days // 7
        except ValueError:
            age = None
        iso2, name = CODES.get(code, (code[:2], code))
        out[code] = {"iso2": iso2, "name": name, "latest": last, "age_weeks": age,
                     "max_excess_recent": max((s["excess_pct"] or 0) for s in series[-4:]), "series": series}
    return out


OWID = "https://ourworldindata.org/grapher/excess-mortality-p-scores-average-baseline.csv?v=1&csvType=full&useColumnShortNames=true"
ISO3 = {"AUS": "AU", "AUT": "AT", "BEL": "BE", "BGR": "BG", "CAN": "CA", "CHE": "CH", "CHL": "CL", "CZE": "CZ", "DEU": "DE",
        "DNK": "DK", "ESP": "ES", "EST": "EE", "FIN": "FI", "FRA": "FR", "GBR": "GB", "GRC": "GR", "HRV": "HR", "HUN": "HU",
        "ISL": "IS", "ISR": "IL", "ITA": "IT", "KOR": "KR", "LTU": "LT", "LUX": "LU", "LVA": "LV", "NLD": "NL", "NOR": "NO",
        "NZL": "NZ", "POL": "PL", "PRT": "PT", "RUS": "RU", "SVK": "SK", "SVN": "SI", "SWE": "SE", "TWN": "TW", "USA": "US",
        "JPN": "JP", "MEX": "MX", "BRA": "BR", "COL": "CO", "PER": "PE", "ZAF": "ZA", "PHL": "PH", "MYS": "MY", "THA": "TH",
        "SGP": "SG", "HKG": "HK", "UKR": "UA", "BLR": "BY", "ROU": "RO", "SRB": "RS", "IRL": "IE", "EGY": "EG", "QAT": "QA",
        "ARG": "AR", "ECU": "EC", "GTM": "GT", "CRI": "CR", "PAN": "PA", "URY": "UY", "MNG": "MN", "KAZ": "KZ", "UZB": "UZ",
        "ARM": "AM", "GEO": "GE", "AZE": "AZ", "MDA": "MD", "MLT": "MT", "CYP": "CY", "TUR": "TR", "IRN": "IR", "OMN": "OM"}


def parse_owid(text, now, history=12):
    rows = list(csv.DictReader(io.StringIO(text.lstrip("\ufeff"))))
    if not rows or "Code" not in rows[0]:
        raise ValueError("OWID CSV not recognised: " + text[:120])
    datecol = next((c for c in ("Day", "date", "Date", "Year") if c in rows[0]), None)
    valcol = [c for c in rows[0] if c not in ("Entity", "Code", datecol)][0]
    by = {}
    for r in rows:
        code = r.get("Code") or ""
        try:
            v = float(r[valcol])
        except (TypeError, ValueError):
            continue
        if len(code) == 3:
            by.setdefault(code, []).append((r[datecol], round(v, 1), r.get("Entity", code)))
    out = {}
    for code, vals in by.items():
        vals.sort()
        series = []
        for d, v, _ in vals[-history:]:
            try:
                y, w, _ = date.fromisoformat(d[:10]).isocalendar()
            except ValueError:
                y, w = None, None
            series.append({"date": d, "year": y, "week": w, "deaths": None, "expected": None, "excess_pct": v})
        last = series[-1]
        try:
            age = (now.date() - date.fromisoformat(last["date"][:10])).days // 7
        except ValueError:
            age = None
        out[code] = {"iso2": ISO3.get(code, code[:2]), "name": vals[-1][2], "latest": last, "age_weeks": age,
                     "max_excess_recent": max((s["excess_pct"] or 0) for s in series[-4:]), "series": series}
    return out


def refresh(now, previous):
    last = (previous or {}).get("generated_at")
    if last and now - datetime.strptime(last, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc) < timedelta(hours=24):
        return None
    try:
        countries, source = parse_stmf(http_get(STMF, timeout=90).decode("utf-8", "replace"), now), "Human Mortality Database (STMF)"
    except ValueError:  # HMD now asks for a login; Our World in Data republishes excess-mortality P-scores
        countries, source = parse_owid(http_get(OWID, timeout=90).decode("utf-8", "replace"), now), "Our World in Data (HMD / World Mortality Dataset)"
    if not countries:
        raise RuntimeError("no countries parsed")
    return {"generated_at": iso(now), "source": source, "countries": countries}


def summary(mort, max_age_weeks=10, threshold=20.0):
    """Countries with recent (not stale) deaths well above normal."""
    hot = []
    for c in (mort or {}).get("countries", {}).values():
        if c.get("age_weeks") is not None and c["age_weeks"] <= max_age_weeks and c["max_excess_recent"] >= threshold:
            hot.append(f"{c['name']} +{c['max_excess_recent']:.0f}%")
    return hot
