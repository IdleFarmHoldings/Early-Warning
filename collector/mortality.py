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
    lines = text.splitlines()
    start = next((i for i, l in enumerate(lines) if l.lower().startswith("countrycode")), None)
    if start is None:
        raise ValueError("STMF header not found")
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


def refresh(now, previous):
    last = (previous or {}).get("generated_at")
    if last and now - datetime.strptime(last, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc) < timedelta(hours=24):
        return None
    countries = parse_stmf(http_get(STMF, timeout=90).decode("utf-8", "replace"), now)
    if not countries:
        raise RuntimeError("no countries parsed from STMF")
    return {"generated_at": iso(now), "source": "Human Mortality Database STMF", "countries": countries}


def summary(mort, max_age_weeks=10, threshold=20.0):
    """Countries with recent (not stale) deaths well above normal."""
    hot = []
    for c in (mort or {}).get("countries", {}).values():
        if c.get("age_weeks") is not None and c["age_weeks"] <= max_age_weeks and c["max_excess_recent"] >= threshold:
            hot.append(f"{c['name']} +{c['max_excess_recent']:.0f}%")
    return hot
