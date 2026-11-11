"""radio-llm-log: keep a searchable log of everything an OpenWebRX+ receiver decodes,
with optional AI summaries.

  python3 -m radio_llm_log run              subscribe to MQTT + annotate (the service)
  python3 -m radio_llm_log replay FILE      feed JSON-lines spots (testing, or a saved capture)
  python3 -m radio_llm_log tail [-n 30]     print the newest entries
  python3 -m radio_llm_log export [--date YYYY-MM-DD] [--format md|jsonl]

Settings come from environment variables (see README):
  RLL_DB, RLL_MQTT_HOST, RLL_MQTT_PORT, RLL_MQTT_TOPIC,
  RLL_LLM_URL, RLL_LLM_MODEL, RLL_LLM_KEY, RLL_AI_KINDS, RLL_AI_INTERVAL
"""
import argparse
import json
import os
import sys
import threading
import time
from datetime import datetime, timezone

from . import llm, store
from .normalize import normalize


def env(name, default=None):
    v = os.environ.get(name)
    return v if v not in (None, "") else default


def cfg():
    return {
        "db": env("RLL_DB", "radio-log.db"),
        "mqtt_host": env("RLL_MQTT_HOST", "localhost"),
        "mqtt_port": int(env("RLL_MQTT_PORT", "1883")),
        "mqtt_topic": env("RLL_MQTT_TOPIC", "openwebrx"),
        "llm_url": env("RLL_LLM_URL"),            # unset = no AI layer
        "llm_model": env("RLL_LLM_MODEL", "default"),
        "llm_key": env("RLL_LLM_KEY"),
        "ai_kinds": env("RLL_AI_KINDS", "voice,morse").split(","),
        "ai_interval": float(env("RLL_AI_INTERVAL", "20")),
    }


def log(msg):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), msg, flush=True)


def ingest_one(conn, payload, received=None):
    try:
        spot = json.loads(payload)
    except (ValueError, TypeError):
        return None
    e = normalize(spot, received or time.time())
    return store.add(conn, e) if e else None


def ai_loop(c, stop):
    """Annotate new voice/Morse entries, newest and flagged first. Never blocks ingest."""
    if not c["llm_url"]:
        log("AI layer off (RLL_LLM_URL not set); rules still run")
        return
    conn = store.connect(c["db"])
    log(f"AI layer on: {c['llm_url']} model={c['llm_model']} kinds={c['ai_kinds']}")
    while not stop.is_set():
        for row in store.pending_ai(conn, c["ai_kinds"]):
            try:
                summary, flags, calls, model = llm.annotate(dict(row), c["llm_url"], c["llm_model"], c["llm_key"])
                if calls:
                    summary = f"{summary} [callsigns: {', '.join(calls)}]"
                store.save_ai(conn, row["id"], summary, flags, model)
            except OSError as ex:     # model server down/slow (URLError, timeout, 5xx): leave pending, retry later
                if getattr(ex, "code", 500) < 500:   # 4xx: this request is bad; retrying won't help
                    store.save_ai(conn, row["id"], None, [], c["llm_model"], error=f"HTTP {ex.code}")
                    continue
                log(f"AI endpoint unavailable ({ex}); will retry")
                break
            except Exception as ex:   # the model answered but unusably: record it once, move on
                store.save_ai(conn, row["id"], None, [], c["llm_model"], error=f"{type(ex).__name__}: {ex}"[:300])
                log(f"AI annotate #{row['id']} failed: {ex}")
        stop.wait(c["ai_interval"])


def run(c):
    try:
        import paho.mqtt.client as mqtt
    except ImportError:
        sys.exit("needs paho-mqtt (Debian/Ubuntu: apt install python3-paho-mqtt; or pip install paho-mqtt)")
    conn = store.connect(c["db"])
    lock = threading.Lock()
    topic = c["mqtt_topic"].rstrip("/") + "/#"

    def on_connect(client, userdata, flags, reason, properties=None):
        log(f"MQTT connected ({reason}); subscribing {topic}")
        client.subscribe(topic)

    def on_message(client, userdata, msg):
        with lock:
            rid = ingest_one(conn, msg.payload)
        if rid:
            row = conn.execute("SELECT mode, freq, text, flags FROM entries WHERE id=?", (rid,)).fetchone()
            if row["flags"] != "[]":
                log(f"FLAG {row['flags']} {row['mode']} {row['freq'] / 1e6:.4f} MHz: {row['text'][:120]}")

    if hasattr(mqtt, "CallbackAPIVersion"):
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="radio-llm-log")
    else:
        client = mqtt.Client(client_id="radio-llm-log")
    client.on_connect, client.on_message = on_connect, on_message
    client.reconnect_delay_set(1, 60)
    stop = threading.Event()
    threading.Thread(target=ai_loop, args=(c, stop), daemon=True, name="ai").start()
    log(f"log database: {os.path.abspath(c['db'])}")
    while True:
        try:
            client.connect(c["mqtt_host"], c["mqtt_port"], keepalive=60)
            break
        except OSError as ex:
            log(f"MQTT broker {c['mqtt_host']}:{c['mqtt_port']} not reachable ({ex}); retrying in 30 s")
            time.sleep(30)
    try:
        client.loop_forever(retry_first_connection=True)
    except KeyboardInterrupt:
        stop.set()


def fmt(row, local=False):
    t = datetime.fromtimestamp(row["ts"], timezone.utc)
    if local:
        t = t.astimezone()
    when = t.strftime("%Y-%m-%d %H:%M:%S %Z")
    head = f"{when}  {row['freq'] / 1e6:10.4f} MHz  {row['mode']:<6}"
    if row["callsign"]:
        head += f"  {row['callsign']}"
    lines = [head, f"    heard (machine-decoded): {row['text']}"]
    if row["expanded"] and row["expanded"] != row["text"]:
        lines.append(f"    shorthand (rule): {row['expanded']}")
    if row["flags"] != "[]":
        lines.append(f"    ALERT (rule): {', '.join(json.loads(row['flags']))}")
    if row["ai_summary"]:
        ai_flags = json.loads(row["ai_flags"] or "[]")
        lines.append(f"    AI summary ({row['ai_model']}, may be wrong): {row['ai_summary']}"
                     + (f"  [AI flags: {', '.join(ai_flags)}]" if ai_flags else ""))
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="radio-llm-log", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run")
    rp = sub.add_parser("replay")
    rp.add_argument("file", help="JSON-lines file of spots, or - for stdin")
    tp = sub.add_parser("tail")
    tp.add_argument("-n", type=int, default=30)
    tp.add_argument("--all", action="store_true", help="include noise and repeats")
    tp.add_argument("--local", action="store_true", help="local time instead of UTC")
    ep = sub.add_parser("export")
    ep.add_argument("--date", help="UTC day YYYY-MM-DD (default today)")
    ep.add_argument("--format", choices=["md", "jsonl"], default="md")
    a = ap.parse_args(argv)
    c = cfg()

    if a.cmd == "run":
        run(c)
    elif a.cmd == "replay":
        conn = store.connect(c["db"])
        src = sys.stdin if a.file == "-" else open(a.file)
        n = sum(1 for line in src if line.strip() and ingest_one(conn, line))
        print(f"stored {n} entries in {c['db']}")
    elif a.cmd == "tail":
        conn = store.connect(c["db"])
        rows = store.recent(conn, hours=None, include_noise=a.all, include_repeats=a.all, limit=a.n)
        for r in reversed(rows):
            print(fmt(r, a.local))
    elif a.cmd == "export":
        conn = store.connect(c["db"])
        day = a.date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
        start = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()
        rows = conn.execute("SELECT * FROM entries WHERE ts>=? AND ts<? AND noise=0 ORDER BY ts",
                            (start, start + 86400)).fetchall()
        if a.format == "jsonl":
            for r in rows:
                d = dict(r)
                d.pop("raw", None)
                print(json.dumps(d))
        else:
            print(f"# Radio log {day} (UTC)\n\nEverything below was decoded by machine and may contain errors. "
                  "ALERT lines come from fixed rules; AI summaries come from a language model.\n")
            for r in rows:
                if r["repeat_of"] is None:
                    print(fmt(r) + "\n")


if __name__ == "__main__":
    main()
