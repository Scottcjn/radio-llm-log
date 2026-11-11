"""Turn one OpenWebRX+ MQTT spot (topic openwebrx/<MODE>, JSON body) into a log entry.

Field names follow OpenWebRX+ 1.2.x: SPEECH {text,freq,timestamp}; CW/RTTY skimmer
{callsign,msg,freq,db,timestamp}; WSJT modes {msg,callsign,locator,db,freq,timestamp};
APRS {source,destination,comment,lat,lon,weather,...}. Unknown modes are kept, not dropped.
"""
import json

WSJT = {"FT8", "FT4", "WSPR", "JT65", "JT9", "FST4", "FST4W", "Q65", "JS8", "MSK144"}


def _weather(w):
    if not isinstance(w, dict):
        return ""
    parts = []
    if "temperature" in w:
        parts.append(f"temp {w['temperature']}")
    if "wind" in w and isinstance(w["wind"], dict):
        parts.append("wind " + " ".join(f"{k} {v}" for k, v in w["wind"].items()))
    for k in ("humidity", "barometricpressure", "rain"):
        if k in w:
            parts.append(f"{k} {w[k]}")
    return "weather: " + ", ".join(str(p) for p in parts) if parts else ""


def normalize(spot, received):
    """-> dict(ts, mode, kind, freq, callsign, text, raw) or None if the spot is empty."""
    if not isinstance(spot, dict):
        return None
    mode = str(spot.get("mode") or "UNKNOWN").upper()
    ts = spot.get("timestamp")
    ts = ts / 1000.0 if isinstance(ts, (int, float)) and ts > 1e11 else (ts or received)
    freq = int(spot.get("freq") or 0)
    callsign = spot.get("callsign") or ""

    if mode == "SPEECH":
        kind, text = "voice", (spot.get("text") or "").strip()
    elif mode == "CW":
        kind, text = "morse", (spot.get("msg") or "").strip()
    elif mode == "RTTY":
        kind, text = "teletype", (spot.get("msg") or "").strip()
    elif mode in WSJT:
        kind, text = "digital", (spot.get("msg") or "").strip()
    elif mode == "APRS":
        kind = "aprs"
        callsign = spot.get("source") or callsign
        bits = [spot.get("comment") or "", _weather(spot.get("weather"))]
        if spot.get("lat") is not None and spot.get("lon") is not None:
            bits.append(f"position {spot['lat']:.4f},{spot['lon']:.4f}"
                        if isinstance(spot["lat"], (int, float)) else "")
        text = " | ".join(b for b in bits if b) or "(position/status packet)"
    else:
        kind = "other"
        text = (spot.get("text") or spot.get("msg") or spot.get("comment") or "").strip()

    if not text and not callsign:
        return None
    return {"ts": float(ts), "mode": mode, "kind": kind, "freq": freq, "callsign": callsign,
            "text": text, "raw": json.dumps(spot, sort_keys=True)[:4000]}
