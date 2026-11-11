"""SQLite log. One row per decode. Rule results and AI results live in separate
columns so a reader always knows which is which."""
import json
import sqlite3
import time

from . import rules

SCHEMA = """
CREATE TABLE IF NOT EXISTS entries (
  id          INTEGER PRIMARY KEY,
  ts          REAL NOT NULL,      -- when it was heard (unix seconds, UTC)
  received    REAL NOT NULL,      -- when this kit got it
  mode        TEXT NOT NULL,      -- SPEECH, CW, FT8, APRS, ...
  kind        TEXT NOT NULL,      -- voice, morse, teletype, digital, aprs, other
  freq        INTEGER,
  callsign    TEXT,
  text        TEXT,               -- exactly what the decoder produced
  expanded    TEXT,               -- rule-based Morse shorthand expansion
  noise       INTEGER DEFAULT 0,  -- 1 = no real content (Whisper silence hallucination, looping)
  repeat_of   INTEGER,            -- id of an earlier, near-identical entry
  flags       TEXT DEFAULT '[]',  -- RULE flags (emergency, severe_weather): deterministic
  ai_summary  TEXT,               -- AI layer (optional), labelled as machine-made
  ai_flags    TEXT,
  ai_model    TEXT,
  ai_ts       REAL,
  ai_error    TEXT,
  raw         TEXT
);
CREATE INDEX IF NOT EXISTS entries_ts ON entries(ts);
CREATE INDEX IF NOT EXISTS entries_pending ON entries(ai_ts, kind);
"""

REPEAT_WINDOW = {"voice": 3 * 3600, "morse": 600, "teletype": 600, "digital": 120, "aprs": 600, "other": 300}
REPEAT_RATIO = {"voice": 0.85}   # others: exact text match


def connect(path):
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    return conn


def find_repeat(conn, e):
    since = e["ts"] - REPEAT_WINDOW.get(e["kind"], 300)
    rows = conn.execute(
        "SELECT id, text, callsign FROM entries WHERE kind=? AND freq BETWEEN ? AND ? AND ts>=? "
        "AND noise=0 ORDER BY ts DESC LIMIT 50",
        (e["kind"], e["freq"] - 500, e["freq"] + 500, since)).fetchall()
    ratio = REPEAT_RATIO.get(e["kind"])
    for r in rows:
        if r["callsign"] != e["callsign"] and e["kind"] != "voice":
            continue
        same = (rules.similar(r["text"], e["text"]) >= ratio) if ratio else (r["text"] == e["text"])
        if same:
            return r["id"]
    return None


def add(conn, e, now=None):
    """Apply the rules to a normalized entry and store it. Returns the new row id."""
    text = e["text"]
    noise = e["kind"] == "voice" and (rules.is_noise(text) or rules.is_looping(text))
    expanded = rules.expand_cw(text) if e["kind"] == "morse" else None
    flags = [] if noise else rules.flags_for(text)
    repeat_of = None if noise else find_repeat(conn, e)
    cur = conn.execute(
        "INSERT INTO entries (ts, received, mode, kind, freq, callsign, text, expanded, noise, repeat_of, flags, raw)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (e["ts"], now or time.time(), e["mode"], e["kind"], e["freq"], e["callsign"], text, expanded,
         int(noise), repeat_of, json.dumps(flags), e.get("raw")))
    conn.commit()
    return cur.lastrowid


def pending_ai(conn, kinds, limit=5, max_age_s=6 * 3600):
    marks = ",".join("?" * len(kinds))
    return conn.execute(
        f"SELECT * FROM entries WHERE ai_ts IS NULL AND noise=0 AND repeat_of IS NULL AND kind IN ({marks}) "
        f"AND ts >= ? ORDER BY (flags != '[]') DESC, ts DESC LIMIT ?",
        (*kinds, time.time() - max_age_s, limit)).fetchall()


def save_ai(conn, entry_id, summary, flags, model, error=None):
    conn.execute("UPDATE entries SET ai_summary=?, ai_flags=?, ai_model=?, ai_ts=?, ai_error=? WHERE id=?",
                 (summary, json.dumps(flags or []), model, time.time(), error, entry_id))
    conn.commit()


def recent(conn, hours=24, include_noise=False, include_repeats=False, limit=500):
    """Newest entries first. hours=None means no time limit."""
    q = "SELECT * FROM entries WHERE ts >= ?"
    if not include_noise:
        q += " AND noise=0"
    if not include_repeats:
        q += " AND repeat_of IS NULL"
    since = 0 if hours is None else time.time() - hours * 3600
    return conn.execute(q + " ORDER BY ts DESC LIMIT ?", (since, limit)).fetchall()
