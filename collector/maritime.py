"""Vessels loitering over known undersea-cable corridors, from live AIS (aisstream.io, free key in the
AISSTREAM_KEY repository secret). Terrestrial AIS coverage is good near coasts and patchy offshore; vessels can
also switch AIS off, so absence of loiterers is not proof of safety."""
import asyncio
import json
import os
from datetime import datetime, timedelta, timezone

from common import CONFIG, iso

WS = "wss://stream.aisstream.io/v0/stream"
MID_FLAGS = {"412": "China", "413": "China", "414": "China", "477": "Hong Kong", "273": "Russia", "445": "North Korea",
             "422": "Iran", "503": "Australia", "416": "Taiwan", "538": "Marshall Islands", "636": "Liberia",
             "351": "Panama", "352": "Panama", "353": "Panama", "354": "Panama", "355": "Panama", "356": "Panama",
             "357": "Panama", "370": "Panama", "371": "Panama", "372": "Panama", "373": "Panama"}


def flag(mmsi):
    return MID_FLAGS.get(str(mmsi)[:3], "Other")


def zone_of(lat, lon):
    for z in CONFIG["maritime"]["zones"]:
        b = z["box"]
        if b[0] <= lat <= b[1] and b[2] <= lon <= b[3]:
            return z["key"]
    return None


def handle(msg, seen, now):
    """Fold one aisstream message into `seen` {mmsi: record}."""
    meta = msg.get("MetaData") or {}
    mmsi = str(meta.get("MMSI") or "")
    lat, lon = meta.get("latitude"), meta.get("longitude")
    if not mmsi or lat is None or lon is None:
        return
    z = zone_of(lat, lon)
    if not z:
        return
    body = (msg.get("Message") or {})
    pr = body.get("PositionReport") or body.get("StandardClassBPositionReport") or {}
    sog = pr.get("Sog")
    rec = seen.setdefault(mmsi, {"mmsi": mmsi, "name": (meta.get("ShipName") or "").strip(), "flag": flag(mmsi)})
    rec.update({"zone": z, "lat": round(lat, 4), "lon": round(lon, 4), "last": iso(now)})
    if sog is not None:
        rec["sog"] = sog
        rec["status"] = pr.get("NavigationalStatus")
    sd = body.get("ShipStaticData") or {}
    if sd.get("Type") is not None:
        rec["type"] = sd.get("Type")


async def _listen(key, seconds, seen):
    import websockets  # installed by the workflow
    boxes = [[[z["box"][0], z["box"][2]], [z["box"][1], z["box"][3]]] for z in CONFIG["maritime"]["zones"]]
    async with websockets.connect(WS, open_timeout=20) as ws:
        await ws.send(json.dumps({"APIKey": key, "BoundingBoxes": boxes,
                                  "FilterMessageTypes": ["PositionReport", "StandardClassBPositionReport", "ShipStaticData"]}))
        end = asyncio.get_event_loop().time() + seconds
        while True:
            left = end - asyncio.get_event_loop().time()
            if left <= 0:
                break
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=left)
            except asyncio.TimeoutError:
                break
            try:
                handle(json.loads(raw), seen, datetime.now(timezone.utc))
            except (ValueError, TypeError):
                continue


def update_tracks(tracks, seen, now, mc):
    """Keep per-vessel history across runs; a loiterer is slow (< max_knots) in one zone for >= min_minutes."""
    for mmsi, r in seen.items():
        t = tracks.get(mmsi)
        slow = r.get("sog") is not None and r["sog"] < mc["max_knots"] and r.get("status") not in (5,)  # 5 = moored
        if t and t["zone"] == r["zone"] and t.get("slow") and slow:
            t.update({**r, "slow": True})
        else:
            tracks[mmsi] = {**r, "first": iso(now), "slow": slow}
    cutoff = iso(now - timedelta(hours=mc["forget_hours"]))
    known = {z["key"] for z in CONFIG["maritime"]["zones"]}  # forget vessels remembered in a zone that is no longer watched
    for m in [m for m, t in tracks.items() if t["last"] < cutoff or t.get("zone") not in known]:
        del tracks[m]
    out = []
    for t in tracks.values():
        mins = (datetime.strptime(t["last"], "%Y-%m-%dT%H:%M:%SZ") - datetime.strptime(t["first"], "%Y-%m-%dT%H:%M:%SZ")).total_seconds() / 60
        if t.get("slow") and mins >= mc["min_minutes"] and t["last"] >= iso(now - timedelta(minutes=40)):
            out.append({**{k: t.get(k) for k in ("mmsi", "name", "flag", "zone", "lat", "lon", "sog", "type")},
                        "minutes": round(mins), "of_interest": t["flag"] in mc["flags_of_interest"]})
    return sorted(out, key=lambda x: (not x["of_interest"], -x["minutes"]))


def fetch_maritime(state):
    key = os.environ.get("AISSTREAM_KEY", "").strip()
    if not key:
        raise RuntimeError("needs a free aisstream.io key in the AISSTREAM_KEY repository secret")
    mc = CONFIG["maritime"]
    seen = {}
    asyncio.run(_listen(key, mc["listen_seconds"], seen))
    now = datetime.now(timezone.utc)
    tracks = state.setdefault("ais_tracks", {})
    loiter = update_tracks(tracks, seen, now, mc)
    zones = {z["key"]: {"name": z["name"], "box": z["box"], "region": z["region"], "vessels_now": 0, "loitering": 0, "of_interest": 0}
             for z in mc["zones"]}
    for r in seen.values():
        zones[r["zone"]]["vessels_now"] += 1
    for l in loiter:
        zones[l["zone"]]["loitering"] += 1
        zones[l["zone"]]["of_interest"] += 1 if l["of_interest"] else 0
    return {"generated_at": iso(now), "messages_vessels": len(seen), "zones": zones, "loitering": loiter[:60]}
