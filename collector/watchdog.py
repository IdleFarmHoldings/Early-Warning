"""Watchdog: is the collector still updating? Restart it if stalled; alert phones if data is over 2 hours old,
and say when it recovers. Runs as its own GitHub workflow so a stuck collector cannot silence it."""
import argparse
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import CONFIG, ROOT, iso, load_json, now_utc, push, save_json  # noqa: E402

RESTART_AFTER = timedelta(minutes=45)
ALERT_AFTER = timedelta(hours=2)
REALERT_EVERY = timedelta(hours=6)


def _ts(s):
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def decide(now, last_update, running, queued, state):
    """Returns (actions, state). actions: subset of {"restart", "alert", "recovered"}."""
    actions = set()
    age = now - last_update if last_update else timedelta(days=99)
    if age > RESTART_AFTER and running == 0 and queued == 0:
        actions.add("restart")
    if age > ALERT_AFTER:
        last = state.get("alerted_at")
        if not last or now - _ts(last) > REALERT_EVERY:
            actions.add("alert")
            state["alerted_at"] = iso(now)
        state["stale"] = True
    elif state.get("stale"):
        actions.add("recovered")
        state["stale"] = False
        state.pop("alerted_at", None)
    state["checked_at"] = iso(now)
    state["data_age_minutes"] = round(age.total_seconds() / 60)
    return actions, state


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--running", type=int, default=0)
    ap.add_argument("--queued", type=int, default=0)
    a = ap.parse_args()
    now = now_utc()
    gen = load_json("data/latest.json", {}).get("generated_at")
    last = _ts(gen) if gen else None
    state = load_json("data/watchdog.json", {})
    actions, state = decide(now, last, a.running, a.queued, state)
    hours = state["data_age_minutes"] / 60
    if "restart" in actions:
        r = subprocess.run(["gh", "workflow", "run", "collector.yml", "--ref", "main"], capture_output=True, text=True)
        state["restarted_at"] = iso(now)
        print("restart:", r.returncode, r.stderr[:200])
    if "alert" in actions:
        push("proactive", "System check: updates have stopped",
             f"The Early Warning collector has not updated for about {hours:.1f} hours. Automatic restart attempted. "
             "Until it recovers, rely on Alert SA, ABC Radio Adelaide 891 AM and official sources.",
             priority=4, tags=["warning"], click=CONFIG["board_url"])
    # The twice-daily analysis should update status.json by ~4:45 am and pm Adelaide time
    upd = load_json("data/status.json", {}).get("updatedAt")
    try:
        a_age = now - datetime.fromisoformat(upd).astimezone(timezone.utc) if upd else None
    except ValueError:
        a_age = None
    if a_age and a_age > timedelta(hours=14):
        last = state.get("analysis_alerted_at")
        if not last or now - _ts(last) > timedelta(hours=12):
            state["analysis_alerted_at"] = iso(now)
            push("proactive", "System check: twice-daily analysis missed",
                 f"The last full analysis was {a_age.total_seconds() / 3600:.0f} hours ago. Automatic checks and alerts "
                 "are still running; the board's assessment may be out of date.", priority=3, tags=["warning"],
                 click=CONFIG["board_url"])
    elif a_age is not None:
        state.pop("analysis_alerted_at", None)
    if "recovered" in actions:
        push("proactive", "System check: updates are back",
             "The Early Warning collector is updating normally again.", priority=2, tags=["white_check_mark"],
             click=CONFIG["board_url"])
    save_json("data/watchdog.json", state)
    print(f"data age {state['data_age_minutes']} min, running={a.running}, queued={a.queued}, actions={sorted(actions)}")


if __name__ == "__main__":
    main()
