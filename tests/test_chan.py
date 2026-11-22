"""4chan chatter: bursts of new threads are tip-offs for the analysis, never phone alerts."""
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "collector"))

import brief  # noqa: E402
import chan  # noqa: E402
from common import iso, now_utc  # noqa: E402


def catalog(now, threads):
    """A catalog.json payload in 4chan's documented shape: pages of thread objects."""
    return [{"page": 1, "threads": [{"no": 1000 + i, "time": int((now - timedelta(hours=h)).timestamp()), "replies": r,
                                     **({"sub": sub} if sub else {}), "com": com, **extra}
                                    for i, (h, r, sub, com, extra) in enumerate(threads)]}]


def test_parse_strips_html_skips_stickies_and_flags_generals():
    now = now_utc()
    th = chan.parse_catalog(catalog(now, [
        (1, 40, "", "Explosions reported in Tel Aviv<br><span class=\"quote\">&gt;&gt;123</span> &amp; sirens", {}),
        (1, 900, "/ukr/ - Ukraine War General #4512", "Previous thread", {}),
        (1, 5, "Board rules", "Read them", {"sticky": 1}),
    ]), "pol")
    assert len(th) == 2
    assert th[0]["title"] == "Explosions reported in Tel Aviv & sirens" and not th[0]["general"]
    assert th[1]["general"]


def test_aggregate_counts_only_new_live_non_general_threads():
    now = now_utc()
    th = chan.parse_catalog(catalog(now, [
        (0.5, 60, "HAPPENING: Iran fires missiles at US base", "", {}),
        (1.0, 30, "Israel says interceptors launched", "", {}),
        (2.0, 20, "Breaking: Hormuz tanker hit", "", {}),
        (5.0, 300, "Iran thread from this morning", "", {}),       # older than the window
        (0.5, 3, "Iran?", "", {}),                                 # too few replies to count
        (0.5, 800, "/mideast/ general", "Iran Israel", {}),        # general
        (0.5, 50, "Where is a good place to plan a holiday", "", {}),  # 'pla' must not match 'place'/'plan'
    ]), "pol")
    agg = chan.aggregate(th, now)
    assert agg["mideast"]["new_threads"] == 3 and agg["mideast"]["breaking"] == 2
    assert agg["taiwan"]["new_threads"] == 0
    assert agg["mideast"]["threads"][0]["per_hour"] == 120  # fastest first


def series_with(metric, value, now, days=6):
    """Every 15 min for `days`, so the hour-of-day baseline has enough samples."""
    rows, t = [], now - timedelta(days=days)
    while t < now - timedelta(minutes=15):
        rows.append({"ts": iso(t), metric: value})
        t += timedelta(minutes=15)
    return rows


def assess_one(topic, n, breaking, series, now):
    raw = {"boards": ["pol"], "threads_seen": 150,
           "topics": {k: {"name": k, "new_threads": 0, "breaking": 0, "replies": 0, "threads": []}
                      for k in [t["key"] for t in chan.TOPICS]}}
    raw["topics"][topic].update({"new_threads": n, "breaking": breaking,
                                 "threads": [{"board": "pol", "title": f"t{i}", "replies": 40, "age_h": 1,
                                              "per_hour": 40, "breaking": False, "url": "u"} for i in range(n)]})
    return chan.assess(raw, series, now)["topics"][topic]


def test_burst_needs_more_than_usual_for_the_hour():
    now = now_utc().replace(second=0, microsecond=0)
    busy = series_with("chan_mideast", 4, now)  # the Middle East is always discussed there
    assert assess_one("mideast", 5, 0, busy, now)["status"] == "normal"
    quiet = series_with("chan_korea", 0, now)
    hit = assess_one("korea", 4, 0, quiet, now)
    assert hit["status"] == "BURST" and len(hit["threads"]) == 4 and "usual" in hit["why"]
    assert assess_one("korea", 2, 0, quiet, now)["status"] == "normal"  # below the floor
    assert "threads" not in assess_one("mideast", 5, 0, busy, now)  # titles kept only for bursts


def test_breaking_words_can_burst_while_baseline_is_learning():
    now = now_utc()
    assert assess_one("taiwan", 3, 2, [], now)["status"] == "BURST"
    assert assess_one("taiwan", 3, 1, [], now)["status"] == "learning baseline"


def test_brief_lists_bursts_only_and_metrics_feed_the_series():
    now = now_utc()
    quiet = series_with("chan_korea", 0, now)
    c = {"boards": ["pol"], "topics": {"korea": assess_one("korea", 4, 0, quiet, now),
                                       "mideast": assess_one("mideast", 1, 0, quiet, now)}}
    b = brief.build({"chan": c}, {})
    assert [x["topic"] for x in b["chan_chatter"]["bursts"]] == ["korea"]
    assert chan.metrics({"topics": {"korea": {"new_threads": 4}}}) == {"chan_korea": 4}


def test_chan_never_alerts_phones():
    src = (Path(chan.__file__)).read_text()
    assert "push(" not in src and "_alert(" not in src and "watch_signals" not in src
